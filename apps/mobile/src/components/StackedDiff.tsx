interface StackedDiffProps {
  before: string;
  after: string;
  removed: string;
  added: string;
  note?: string;
}

function highlight(text: string, value: string, mode: "before" | "after") {
  const at = text.indexOf(value);
  if (at < 0) return text;
  const Change = mode === "before" ? "del" : "ins";
  return <>{text.slice(0, at)}<Change>{value}</Change>{text.slice(at + value.length)}</>;
}

function parsedBody(value: string) {
  if (!value.includes("<") || typeof DOMParser === "undefined") return null;
  return new DOMParser().parseFromString(value, "text/html").body;
}

function diffContent(value: string, changed: string, mode: "before" | "after") {
  const body = parsedBody(value);
  const source = body?.querySelector("table");
  if (source) {
    const label = mode === "before" ? "Before table" : "After table";
    return (
      <div className="diff-table-scroll">
        <table className="diff-table" aria-label={label}>
          <tbody>
            {Array.from(source.rows).map((row, rowIndex) => (
              <tr key={rowIndex}>
                {Array.from(row.cells).map((cell, cellIndex) => cell.tagName === "TH" ? (
                  <th key={cellIndex} scope="col">{cell.textContent?.trim()}</th>
                ) : (
                  <td key={cellIndex}>{cell.textContent?.trim()}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  const text = body?.textContent?.trim() || value;
  const changedText = parsedBody(changed)?.textContent?.trim() || changed;
  return <p>{highlight(text, changedText, mode)}</p>;
}

export function StackedDiff({ before, after, removed, added, note }: StackedDiffProps) {
  return (
    <section className="diff-block" aria-labelledby="diff-heading">
      <h2 className="section-label" id="diff-heading">Proposed register entry</h2>
      <div className="diff-grid">
        <div className="diff-part diff-part--before">
          <strong>Before</strong>
          {diffContent(before, removed, "before")}
        </div>
        <div className="diff-part diff-part--after">
          <strong>After</strong>
          {diffContent(after, added, "after")}
          {note ? <small>{note}</small> : null}
        </div>
      </div>
    </section>
  );
}
