import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { MessageContent } from "./MessageContent";

function show(text: string, onCitationClick = vi.fn()) {
  const numbers = new Map<string, number>();
  const citationNumber = (id: string) => {
    if (!numbers.has(id)) numbers.set(id, numbers.size + 1);
    return numbers.get(id)!;
  };
  return render(<MessageContent text={text} citationNumber={citationNumber} onCitationClick={onCitationClick} />);
}

describe("MessageContent", () => {
  it("渲染标题、列表、引用块、粗体和段落", () => {
    const { container } = show("## 评估结果\n普通段落\n- 第一条\n- **重点**\n1. 编号\n2、顿号编号\n> 免责声明");
    expect(container.querySelector("h3")).toHaveTextContent("评估结果");
    expect(container.querySelector("p")).toHaveTextContent("普通段落");
    expect(container.querySelectorAll("ul li")).toHaveLength(4);
    expect(container.querySelector("li strong")).toHaveTextContent("重点");
    expect(container.querySelector("blockquote")).toHaveTextContent("免责声明");
  });

  it("空行不产生元素，列表被其他内容隔开时分成两个", () => {
    const { container } = show("- a\n\n普通\n- b");
    expect(container.querySelectorAll("ul")).toHaveLength(2);
    expect(container.querySelectorAll("p")).toHaveLength(1);
  });

  it("引用渲染成按钮，同一 id 编号相同，点击回传 id", async () => {
    const onClick = vi.fn();
    show("- 限盐 [cite:salt_reduction]\n- 运动 [cite:physical_activity][cite:salt_reduction]", onClick);
    const salt = screen.getAllByRole("button", { name: "查看引用来源 salt_reduction" });
    expect(salt).toHaveLength(2);
    expect(salt[0]).toHaveTextContent("1");
    expect(salt[1]).toHaveTextContent("1");
    expect(salt[0]).toHaveClass("citation");
    expect(screen.getByRole("button", { name: "查看引用来源 physical_activity" })).toHaveTextContent("2");
    await userEvent.click(salt[0]);
    expect(onClick).toHaveBeenCalledWith("salt_reduction");
  });

  it("粗体里的引用也能渲染", () => {
    show("**重点 [cite:lipids]**");
    expect(screen.getByRole("button", { name: "查看引用来源 lipids" })).toBeInTheDocument();
  });

  it("模型输出的 HTML 只当文本显示", () => {
    const { container } = show('<img src=x onerror="alert(1)"> **<script>alert(2)</script>**');
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container).toHaveTextContent('<img src=x onerror="alert(1)">');
    expect(container).toHaveTextContent("<script>alert(2)</script>");
  });

  it("不完整的标记原样显示", () => {
    const { container } = show("**没闭合 [cite:] 普通");
    expect(container).toHaveTextContent("**没闭合 [cite:] 普通");
  });
});
