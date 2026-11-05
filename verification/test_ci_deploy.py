"""使用真实 Git 仓库和模拟 Docker 验证部署边界，不连接演示服务器。"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.origin = self.base / "origin"
        self.server = self.base / "server"
        self.origin.mkdir()
        self.git(self.origin, "init", "-b", "main")
        self.git(self.origin, "config", "user.email", "test@example.invalid")
        self.git(self.origin, "config", "user.name", "部署测试")
        (self.origin / "ops").mkdir()
        (self.origin / ".gitignore").write_text(".env\n")
        (self.origin / "docker-compose.yml").write_text("services: {}\n")
        (self.origin / "Caddyfile").write_text("原始配置\n")
        (self.origin / "ops/compose.runtime.yml").write_text("services: {}\n")
        (self.origin / "ops/release.sh").write_text(
            'printf "%s\\n" "$2" >> "$DEPLOY_TEST_RELEASES"\n'
        )
        self.first = self.commit("初始版本")
        self.git(self.base, "clone", str(self.origin), str(self.server))
        (self.server / ".env").write_text("测试配置，不能出现在输出中\n")
        self.log = self.base / "docker.log"
        self.releases = self.base / "release.log"

    def git(self, cwd, *args):
        return subprocess.check_output(
            ["git", "-C", str(cwd), *args], text=True, stderr=subprocess.DEVNULL
        ).strip()

    def commit(self, title):
        self.git(self.origin, "add", ".")
        self.git(self.origin, "commit", "-m", title, "--allow-empty")
        return self.git(self.origin, "rev-parse", "HEAD")

    def run_deploy(self, command, pull_fails=False):
        # shell 函数仅替换外部 Docker 与锁调用，Git 历史和脚本本身按真实方式运行。
        harness = r'''
docker() {
  printf '%s\n' "$*" >> "$DEPLOY_TEST_LOG"
  if [[ "$1" == pull && "$DEPLOY_TEST_PULL_FAIL" == 1 ]]; then return 1; fi
}
flock() { return 0; }
source "$1"
'''
        env = dict(os.environ, HEALTHPILOT_DEPLOY_DIR=str(self.server),
                   SSH_ORIGINAL_COMMAND=command, DEPLOY_TEST_LOG=str(self.log),
                   DEPLOY_TEST_RELEASES=str(self.releases),
                   DEPLOY_TEST_PULL_FAIL=str(int(pull_fails)))
        result = subprocess.run(
            ["bash", "-c", harness, "bash", str(ROOT / "ops/ci-deploy.sh")],
            env=env, text=True, capture_output=True,
        )
        self.assertNotIn("测试配置，不能出现在输出中", result.stdout + result.stderr)
        return result

    def test_check_does_not_pull_or_release(self):
        result = self.run_deploy("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("pull", self.log.read_text())
        self.assertFalse(self.releases.exists())

    def test_deploys_requested_sha_even_when_main_has_newer_commits(self):
        target = self.commit("待部署版本")
        self.commit("稍后推送的版本")
        result = self.run_deploy(f"deploy {target}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(self.server, "rev-parse", "HEAD"), target)
        self.assertEqual(self.releases.read_text().strip(), f"ghcr.io/newuranus/health-agent:sha-{target}")

    def test_pull_failure_keeps_current_revision(self):
        result = self.run_deploy(f"deploy {self.commit('新版本')}", pull_fails=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git(self.server, "rev-parse", "HEAD"), self.first)
        self.assertFalse(self.releases.exists())

    def test_old_build_does_not_overwrite_newer_release(self):
        target = self.commit("新版本")
        self.assertEqual(self.run_deploy(f"deploy {target}").returncode, 0)
        result = self.run_deploy(f"deploy {self.first}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("跳过过期部署", result.stdout)
        self.assertEqual(len(self.releases.read_text().splitlines()), 1)

    def test_rejects_shell_commands_and_other_branch(self):
        for command in ("id", f"deploy {self.first}; id", "deploy main", ""):
            with self.subTest(command=command):
                self.assertEqual(self.run_deploy(command).returncode, 2)
        self.git(self.origin, "checkout", "-b", "feature")
        target = self.commit("非主分支")
        self.git(self.origin, "checkout", "main")
        self.assertNotEqual(self.run_deploy(f"deploy {target}").returncode, 0)
        self.assertFalse(self.releases.exists())

    def test_preserves_server_caddy_customization(self):
        (self.server / "Caddyfile").write_text("服务器配置\n")
        target = self.commit("应用版本")
        self.assertEqual(self.run_deploy(f"deploy {target}").returncode, 0)
        self.assertEqual((self.server / "Caddyfile").read_text(), "服务器配置\n")
        (self.origin / "Caddyfile").write_text("远端新配置\n")
        target = self.commit("冲突的配置")
        self.assertNotEqual(self.run_deploy(f"deploy {target}").returncode, 0)
        self.assertEqual(len(self.releases.read_text().splitlines()), 1)

    def test_rejects_uncommitted_server_changes(self):
        (self.server / "unexpected.txt").write_text("本地修改\n")
        self.assertNotEqual(self.run_deploy(f"deploy {self.first}").returncode, 0)
        self.assertFalse(self.releases.exists())


if __name__ == "__main__":
    unittest.main()
