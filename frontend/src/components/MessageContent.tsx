import type { JSX, ReactNode } from "react";

interface Props {
  text: string;
  citationNumber: (id: string) => number;
  onCitationClick: (id: string) => void;
}

export function MessageContent({ text, citationNumber, onCitationClick }: Props): JSX.Element {
  function inline(value: string): ReactNode[] {
    const parts: ReactNode[] = [];
    const pattern = /\*\*(.+?)\*\*|\[cite:([\w-]+)\]/g;
    let offset = 0;
    for (const match of value.matchAll(pattern)) {
      parts.push(value.slice(offset, match.index));
      if (match[1] !== undefined) {
        parts.push(<strong key={match.index}>{inline(match[1])}</strong>);
      } else {
        const id = match[2];
        parts.push(
          <button key={match.index} type="button" className="citation"
            title={`查看引用来源 ${id}`} aria-label={`查看引用来源 ${id}`}
            onClick={() => onCitationClick(id)}>{citationNumber(id)}</button>,
        );
      }
      offset = match.index + match[0].length;
    }
    parts.push(value.slice(offset));
    return parts;
  }

  const blocks: ReactNode[] = [];
  let items: ReactNode[] = [];
  function finishList() {
    if (items.length) blocks.push(<ul key={blocks.length}>{items}</ul>);
    items = [];
  }

  for (const line of text.split(/\r?\n/)) {
    const item = /^\s*(?:- |\d+\.\s+|\d+、\s*)(.+)$/.exec(line);
    if (item) {
      items.push(<li key={items.length}>{inline(item[1])}</li>);
      continue;
    }
    finishList();
    if (/^#{1,3} /.test(line)) {
      blocks.push(<h3 key={blocks.length}>{inline(line.replace(/^#{1,3} /, ""))}</h3>);
    } else if (/^> ?/.test(line)) {
      blocks.push(<blockquote key={blocks.length}>{inline(line.replace(/^> ?/, ""))}</blockquote>);
    } else if (line.trim()) {
      blocks.push(<p key={blocks.length}>{inline(line)}</p>);
    }
  }
  finishList();
  return <>{blocks}</>;
}
