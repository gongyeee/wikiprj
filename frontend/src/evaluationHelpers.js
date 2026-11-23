export const statusLabels = {
  queued: "排队中", running: "运行中", completed: "已完成", cancelled: "已取消", interrupted: "已中断",
  pending: "待处理", scored: "已评分", answer_failed: "回答失败", score_failed: "评分失败",
  accepted: "评分合理", adjusted: "已调整", rerun: "需重跑", enabled: "启用", disabled: "停用",
  answer: "普通问答", refusal: "应拒答", create: "新增", update: "更新", skip: "跳过", error: "错误", move: "移动", copy: "复制",
};

// Excel clipboard TSV permits quoted multiline cells and doubled quotation marks.
export function parseTsv(text) {
  const rows = [];
  let row = [], value = "", quoted = false, closed = false;
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (quoted) {
      if (char === '"' && text[i + 1] === '"') { value += '"'; i++; }
      else if (char === '"') { quoted = false; closed = true; }
      else value += char;
    } else if (char === '"' && !value && !closed) quoted = true;
    else if (char === "\t") { row.push(value); value = ""; closed = false; }
    else if (char === "\n" || char === "\r") {
      if (char === "\r" && text[i + 1] === "\n") i++;
      row.push(value); rows.push(row); row = []; value = ""; closed = false;
    } else {
      if (closed) throw new Error("引号结束后只能是制表符或换行，请检查粘贴内容");
      value += char;
    }
  }
  if (quoted) throw new Error("粘贴内容存在未闭合的引号");
  row.push(value); rows.push(row);
  return rows.filter(cells => cells.some(cell => cell.trim()));
}

export function pastedCases(text) {
  const fields = ["question", "expected", "category", "scene", "kind", "reference_names", "reference_paths"];
  const rows = parseTsv(text);
  if (rows[0]?.[0]?.trim() === "问题") rows.shift();
  if (!rows.length || rows.length > 2000) throw new Error("每批粘贴 1～2000 条问答");
  return rows.map((row, i) => {
    if (row.length > fields.length) throw new Error(`第 ${i + 1} 行超过 7 列`);
    return Object.fromEntries(fields.map((key, col) => [key, row[col] || ""]));
  });
}
