# 前端规格

用 React + TypeScript + Vite 重写 `web/` 下的旧页面。旧代码在 git 里：`git show 5c7822b:web/app.js`、`git show 5c7822b:web/index.html`。**旧页面的功能和文案都要保留**，除非本文另有说明。视觉沿用旧样式，`frontend/src/styles.css` 是旧 CSS 的副本（压成了一行），格式化成可读形式后按需修改。

后端接口不变，见 `docs/DESIGN.md` 和 `docs/DESIGN_V2.md` 的事件流和 HTTP 接口章节。

## 工程

- 源码在 `frontend/src/`，构建输出到 `web/`（`npm run build`，已配置 `base: /static/`），构建结果提交到 git
- 不引入路由、状态管理、UI 组件库或 Markdown 库。用 `useReducer` 管理会话状态
- **全程不用 `dangerouslySetInnerHTML`**。消息文本解析成 React 元素，外部文本一律作为文本节点渲染

## 必须提供的模块（测试直接依赖）

### `src/lib/sse.ts`

```ts
export function createSseParser(onEvent: (event: ServerEvent) => void): {
  push(chunk: string): void; // 接收任意切分的文本片段
  flush(): void;             // 流结束时处理缓冲区里剩余的事件
};
```

事件以空行分隔，兼容 `\r\n`。忽略不以 `data:` 开头的行（如 SSE 注释）。一个事件可以有多行 `data:`，用 `\n` 拼接后解析 JSON。JSON 解析失败抛错。

### `src/lib/progress.ts`

```ts
export function progressLabel(node: string | null, data?: Record<string, unknown>): string;
```

根据**刚完成**的节点，返回**下一步正在做什么**：

| node | 文案 |
|---|---|
| `null`（刚发送） | `正在连接健康助手…` |
| `safety_check` | `正在整理健康档案…` |
| `intake` | `正在规划下一步…` |
| `supervisor` | 按 `data.route`：`assessment_agent` → `正在计算健康风险…`；`qa_agent` → `正在查找指南并回答，通常需要 5–15 秒…`；`simulation_agent` → `正在模拟干预效果…`；`checkin_agent` → `正在对比复查结果…`；`intake_agent` → `正在整理需要补充的信息…` |
| `risk_assess` | `正在检索指南依据…` |
| `retrieve` | `正在生成干预建议，通常需要 10–20 秒…` |
| `advise` | `正在校验建议与引用…` |
| `verify` | `data.ok === false` 时 `引用校验未通过，正在重新生成…`，否则 `正在制定随访计划…` |
| 其他 | `正在处理…` |

### `src/components/MessageContent.tsx`

```tsx
export function MessageContent(props: {
  text: string;
  citationNumber: (id: string) => number;
  onCitationClick: (id: string) => void;
}): JSX.Element;
```

支持的语法同旧 `markdown()`：`#`～`###` 标题渲染为 `h3`，`- ` 和 `1. `/`1、` 渲染为 `ul > li`，`> ` 渲染为 `blockquote`，`**粗体**` 渲染为 `strong`，其余非空行渲染为 `p`。`[cite:id]` 渲染为按钮，文字是 `citationNumber(id)`，`aria-label` 为 `查看引用来源 {id}`，class 含 `citation`。

### `src/App.tsx`

默认导出 `App` 组件，不接收 props。

## 页面行为（`src/App.test.tsx` 验收）

- 顶部显示模式：`/api/health` 的 `mock` 为真显示 `Mock 演示模式`，否则 `千问真实模式`，请求失败显示 `服务暂不可用`
- 初始显示欢迎语（含 `你好，我是 HealthPilot`）
- 四个示例按钮沿用旧文案和旧的发送内容
- 发送：POST `/api/chat`，body 是 `{thread_id, user_id, message}`。Enter 发送，Shift+Enter 换行，输入法组合中（`isComposing`）不发送
- 处理中：发送和示例按钮禁用，发送按钮显示 `处理中…`，并显示一个带 `role="status"` 的进度提示，内容来自 `progressLabel`
- 每个 node 事件在轨迹面板加一张卡片，内容同旧 `addNode`（节点中文名、耗时、所属 Agent、工具调用、来源、校验结果、降级和回退提示）。轨迹卡片容器的 class 含 `trace-card`，每轮之前有 `第 N 轮对话` 分隔
- `intake` 事件更新健康档案卡；`cvd_risk` 工具结果更新风险仪表（如 `13.5%`、`中风险`）。**数值要四舍五入到 1 位小数，13.45 显示 `13.5%`**（旧代码已处理，注意 toFixed 的浮点问题）
- 点击引用按钮：确保轨迹面板展开，给对应来源卡片（class 含 `source-card`）加 `highlight`，并滚动到它。引用编号按首次出现的顺序递增，新会话重置
- 收到 `error` 事件，或流结束时没有收到 `done` 和 `message`，在对话里显示错误气泡（class 含 `error`），按钮恢复可用；网络异常同样处理
- `新会话`：换 `thread_id`，保留 `user_id`，清空对话、轨迹、档案和风险，重新显示欢迎语。`清除身份`：另外再换 `user_id`
- `user_id` 存在 `localStorage` 的 `healthpilot-user`，读写都要 try/catch，存储不可用时页面照常工作
- 轨迹面板可折叠，按钮文案 `收起轨迹` / `展开轨迹`

## 布局调整（相对旧页面）

**对话框要放在第一屏。** 1024×768 的窗口下，不滚动页面就能看到输入框和示例按钮：

- 去掉大标题区（"让健康，有迹可循"那一块）
- 健康档案和风险仪表压缩成一条紧凑的概览，放在对话区上方
- 对话区和轨迹面板占满剩余高度，各自内部滚动
- 手机宽度（375px）下单列排列，无横向滚动，轨迹面板在对话下方
