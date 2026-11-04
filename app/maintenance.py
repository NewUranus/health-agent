"""PostgreSQL 加密备份与隔离恢复命令；不覆盖现有数据库。"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from dotenv import load_dotenv
from app.auth import ROOT

TABLES = ("hp_users", "hp_tasks", "hp_task_events", "hp_audit", "hp_conversations", "hp_model_usage",
          "knowledge_documents", "knowledge_chunks", "knowledge_archives")


def run(command, env=None):
    result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # 数据库工具异常可能含连接信息，不能直接打印。
        raise RuntimeError(f"{Path(command[0]).name} 执行失败（退出码 {result.returncode}）。请检查工具版本、权限和目标状态。")


def environment(url, database=None):
    fields = conninfo_to_dict(url)
    env = os.environ.copy()
    mapping = {"host": "PGHOST", "hostaddr": "PGHOSTADDR", "port": "PGPORT", "user": "PGUSER", "password": "PGPASSWORD",
        "dbname": "PGDATABASE", "sslmode": "PGSSLMODE", "sslrootcert": "PGSSLROOTCERT", "sslcert": "PGSSLCERT", "sslkey": "PGSSLKEY",
        "connect_timeout": "PGCONNECT_TIMEOUT", "options": "PGOPTIONS", "service": "PGSERVICE", "passfile": "PGPASSFILE", "application_name": "PGAPPNAME"}
    for key, value in fields.items():
        if key not in mapping:
            raise ValueError("备份连接包含未支持的选项，请通过 PostgreSQL 服务配置文件设置连接。")
        env[mapping[key]] = value
    if database:
        env["PGDATABASE"] = database
    env.setdefault("PGCONNECT_TIMEOUT", "10")
    return env


def counts(conn):
    result = {}
    for table in TABLES:
        if conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0]:
            result[table] = conn.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table))).fetchone()[0]
    return result


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def backup(url, output, passphrase):
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError("目标文件已存在，拒绝覆盖。")
    with tempfile.TemporaryDirectory(prefix="healthpilot-backup-") as temporary:
        folder = Path(temporary)
        dump = folder / "database.dump"
        with psycopg.connect(url) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot_id = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
            manifest = {"format": 1, "created_at": datetime.now(timezone.utc).isoformat(), "counts": counts(conn), "release": os.getenv("HEALTH_AGENT_RELEASE", "local")}
            run(["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--snapshot", snapshot_id, "--file", str(dump)], environment(url))
        manifest["sha256"] = digest(dump)
        metadata = folder / "manifest.json"
        metadata.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        archive = folder / "backup.tar"
        with tarfile.open(archive, "w") as bundle:
            bundle.add(dump, arcname="database.dump")
            bundle.add(metadata, arcname="manifest.json")
        # 加密结果先写目标同目录的私有临时文件，完成后才公开最终文件名。
        fd, temporary_name = tempfile.mkstemp(prefix=".healthpilot-", dir=output.parent)
        os.close(fd)
        try:
            run(["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--passphrase-file", str(passphrase),
                 "--symmetric", "--cipher-algo", "AES256", "--output", temporary_name, str(archive)])
            os.link(temporary_name, output)
        finally:
            Path(temporary_name).unlink(missing_ok=True)
    print("加密备份已完成：" + str(output))


def restore(url, source, passphrase, target):
    if not re.fullmatch(r"hp_restore_[a-z0-9_]{1,40}", target):
        raise ValueError("恢复库名称必须以 hp_restore_ 开头，仅含小写字母、数字和下划线。")
    with tempfile.TemporaryDirectory(prefix="healthpilot-restore-") as temporary:
        folder = Path(temporary)
        archive = folder / "backup.tar"
        run(["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--passphrase-file", str(passphrase),
             "--output", str(archive), "--decrypt", str(source)])
        with tarfile.open(archive, "r") as bundle:
            if sorted(bundle.getnames()) != ["database.dump", "manifest.json"]:
                raise ValueError("备份文件结构无效。")
            for name in ("database.dump", "manifest.json"):
                member = bundle.getmember(name)
                if not member.isfile() or (name == "manifest.json" and member.size > 1000000):
                    raise ValueError("备份成员无效。")
                stream = bundle.extractfile(member)
                if stream is None:
                    raise ValueError("无法读取备份内容。")
                with stream, (folder / name).open("wb") as output:
                    shutil.copyfileobj(stream, output)
        manifest = json.loads((folder / "manifest.json").read_text())
        if manifest.get("format") != 1 or manifest.get("sha256") != digest(folder / "database.dump"):
            raise ValueError("备份版本或摘要不一致，停止恢复。")
        if not isinstance(manifest.get("counts"), dict) or set(manifest["counts"]) - set(TABLES):
            raise ValueError("备份清单无效。")
        with psycopg.connect(url, autocommit=True) as conn:
            if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target,)).fetchone():
                raise ValueError("恢复目标已存在，拒绝覆盖。")
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
        run(["pg_restore", "--exit-on-error", "--single-transaction", "--no-owner", "--no-acl", "--dbname", target, str(folder / "database.dump")], environment(url, target))
        fields = conninfo_to_dict(url)
        fields["dbname"] = target
        with psycopg.connect(**fields) as conn:
            restored = counts(conn)
        if restored != manifest["counts"]:
            raise RuntimeError("恢复后核心表计数与一致性快照不符，目标库已保留供排查。")
        # 恢复环境不能复用旧会话或自动重放备份时尚未完成的操作。
        with psycopg.connect(**fields) as conn:
            for table in ("hp_sessions", "hp_workers", "hp_task_slots"):
                if conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0]:
                    conn.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))
            if "hp_tasks" in restored:
                pending = conn.execute("SELECT id FROM hp_tasks WHERE status IN ('pending', 'running')").fetchall()
                for (ident,) in pending:
                    conn.execute("UPDATE hp_tasks SET status = 'interrupted', error_code = 'RESTORE_REVIEW_REQUIRED', worker_token = NULL, lease_until = NULL, updated_at = %s WHERE id = %s", (time.time_ns(), ident))
                    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM hp_task_events WHERE task_id = %s", (ident,)).fetchone()[0]
                    for offset, event in enumerate([{"type": "error", "message": "数据来自恢复快照，请人工核对已有记录后再继续。", "code": "RESTORE_REVIEW_REQUIRED"}, {"type": "done"}], 1):
                        conn.execute("INSERT INTO hp_task_events VALUES (%s, %s, %s)", (ident, seq + offset, json.dumps(event, ensure_ascii=False)))
            if conn.execute("SELECT to_regclass('hp_system_settings')").fetchone()[0]:
                conn.execute("UPDATE hp_system_settings SET value = '1' WHERE name = 'admission_paused'")
            if conn.execute("SELECT to_regclass('hp_approvals')").fetchone()[0]:
                conn.execute("UPDATE hp_approvals SET status = 'invalidated', error_code = 'RESTORE_REVIEW_REQUIRED' WHERE status = 'waiting_human'")
                conn.execute("UPDATE hp_approvals SET status = 'uncertain', error_code = 'RESTORE_REVIEW_REQUIRED' WHERE status = 'executing'")
            if "hp_audit" in restored:
                conn.execute("INSERT INTO hp_audit VALUES (%s, %s, %s, %s, %s, %s, %s)", (uuid.uuid4().hex, int(time.time()), "system", "restore.fenced", target, uuid.uuid4().hex, 200))
    print("隔离恢复及核心表计数核对完成：" + target + "。尚未切换业务连接。")


def main():
    parser = argparse.ArgumentParser(description="HealthPilot 加密备份和隔离恢复")
    parser.add_argument("action", choices=["backup", "restore"])
    parser.add_argument("--passphrase-file", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--target")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    url = os.getenv("DATABASE_URL")
    if not url:
        parser.error("完整备份要求 PostgreSQL；内存知识和检查点无法可靠备份。")
    passphrase = args.passphrase_file.resolve()
    if not passphrase.is_file() or passphrase.stat().st_mode & 0o077 or passphrase.stat().st_size == 0:
        parser.error("口令文件须存在、非空且仅当前用户可读（权限 600）。")
    for executable in ("gpg", "pg_dump" if args.action == "backup" else "pg_restore"):
        if not shutil.which(executable):
            parser.error(f"未安装 {executable}，请由部署方安装对应工具。")
    os.umask(0o077)
    try:
        if args.action == "backup":
            if not args.output:
                parser.error("备份需要 --output 路径。")
            backup(url, args.output, passphrase)
        else:
            if not args.input or not args.target:
                parser.error("恢复需要 --input 备份文件和 --target 隔离库名。")
            restore(url, args.input, passphrase, args.target)
    except (ValueError, RuntimeError) as exc:
        parser.exit(1, str(exc) + "\n")
    except Exception:
        parser.exit(1, "维护操作未完成，请核对工具、连接权限与磁盘空间；恢复目标如已创建将保留，不覆盖其他数据库。\n")


if __name__ == "__main__":
    main()
