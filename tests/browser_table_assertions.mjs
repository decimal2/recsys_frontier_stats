// Self-contained: pass this function to Runtime.evaluate or page.evaluate.
export function collectTableGeometry() {
  const article = document.querySelector('.paper-fulltext');
  const issues = [];
  const metrics = {tables: 0, outerTables: 0, nestedTables: 0, cells: 0, mergedCells: 0,
    numericCellsChecked: 0, numericTokensChecked: 0, horizontalScrollTables: 0, captionsChecked: 0};
  if (!article) return {issues: [{kind: 'paper_body_missing'}], metrics};
  const rect = element => {
    const r = element.getBoundingClientRect();
    return {left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height};
  };
  const tables = [...article.querySelectorAll('.ltx_tabular')];
  const identity = table => ({table: tables.indexOf(table),
    selector: table.id ? `[id=${JSON.stringify(table.id)}]` :
      table.closest('figure[id]') ? `[id=${JSON.stringify(table.closest('figure[id]').id)}] .ltx_tabular` : '.ltx_tabular'});
  const add = (kind, table, detail = {}) => issues.push({kind, ...identity(table), ...detail});
  const lineCount = rectangles => {
    const lines = [];
    for (const r of rectangles.filter(r => r.width > 0 && r.height > 0).sort((a, b) => a.top - b.top)) {
      const line = lines.find(line => Math.min(line.bottom, r.bottom) - Math.max(line.top, r.top) > 1);
      if (line) { line.top = Math.min(line.top, r.top); line.bottom = Math.max(line.bottom, r.bottom); }
      else lines.push({top: r.top, bottom: r.bottom});
    }
    return lines.length;
  };
  const rangesForText = element => {
    const ranges = [];
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (!node.textContent.trim() || node.parentElement.closest('math,annotation,script,style')) continue;
      const range = document.createRange(); range.selectNodeContents(node);
      ranges.push(...range.getClientRects());
    }
    return ranges;
  };
  const numericCell = /^[+\-−]?\d+(?:[.,]\d+)*(?:[eE][+\-]?\d+)?(?:%|[KMB])?$/;
  for (const table of tables) {
    metrics.tables++;
    if (table.tagName !== 'TABLE') { add('nonsemantic_table', table, {tag: table.tagName}); continue; }
    const box = rect(table);
    if (!box.width || !box.height) add('hidden_or_empty_table', table, {box});
    if (getComputedStyle(table).display !== 'table') add('table_display_changed', table,
      {display: getComputedStyle(table).display});
    const outer = !table.parentElement.closest('.ltx_tabular');
    if (outer) {
      metrics.outerTables++;
      const viewport = table.parentElement;
      if (!viewport.classList.contains('paper-table-scroll')) add('missing_table_viewport', table);
      else {
        const vr = rect(viewport), style = getComputedStyle(viewport);
        if (viewport.scrollWidth > viewport.clientWidth + 2) {
          metrics.horizontalScrollTables++;
          if (!['auto', 'scroll'].includes(style.overflowX)) add('wide_table_not_scrollable', table,
            {overflowX: style.overflowX, clientWidth: viewport.clientWidth, scrollWidth: viewport.scrollWidth});
        }
        if (viewport.scrollHeight > viewport.clientHeight + 2 ||
            box.bottom > vr.top + viewport.clientTop + viewport.clientHeight + 2 || box.top < vr.top - 2) {
          add('table_vertical_clipping', table, {box, viewport: vr, clientHeight: viewport.clientHeight,
            scrollHeight: viewport.scrollHeight, overflowY: style.overflowY});
        }
      }
      for (let parent = table.parentElement; parent && parent !== article; parent = parent.parentElement) {
        const style = getComputedStyle(parent);
        if (['auto', 'scroll', 'hidden', 'clip'].includes(style.overflowY) && parent.scrollHeight > parent.clientHeight + 2) {
          add('ancestor_vertical_clipping', table, {ancestor: parent.className,
            clientHeight: parent.clientHeight, scrollHeight: parent.scrollHeight, overflowY: style.overflowY});
        }
        if (parent !== table.parentElement && parent.scrollWidth > parent.clientWidth + 2 &&
            ['auto', 'scroll'].includes(style.overflowX)) {
          add('horizontal_scroll_outside_table_viewport', table, {ancestor: parent.className,
            clientWidth: parent.clientWidth, scrollWidth: parent.scrollWidth});
        }
      }
    } else {
      metrics.nestedTables++;
      if (!table.closest('td,th')) add('nested_table_outside_cell', table);
      if (table.parentElement.classList.contains('paper-table-scroll')) add('nested_table_separate_viewport', table);
    }
    for (const row of table.rows) {
      if (getComputedStyle(row).display !== 'table-row') add('row_display_changed', table,
        {row: row.rowIndex, display: getComputedStyle(row).display});
      for (const cell of row.cells) {
        metrics.cells++;
        if (getComputedStyle(cell).display !== 'table-cell') add('cell_display_changed', table,
          {row: row.rowIndex, cell: cell.cellIndex, display: getComputedStyle(cell).display});
        if (cell.colSpan > 1 || cell.rowSpan > 1) metrics.mergedCells++;
        for (const [attribute, property] of [['colspan', 'colSpan'], ['rowspan', 'rowSpan']]) {
          if (cell.hasAttribute(attribute) && Number(cell.getAttribute(attribute)) !== cell[property]) {
            add('merged_cell_attribute_changed', table, {attribute, requested: cell.getAttribute(attribute), actual: cell[property]});
          }
        }
        const text = cell.textContent.trim();
        if (!cell.querySelector('math,.ltx_tabular') && numericCell.test(text)) {
          metrics.numericCellsChecked++;
          const lines = lineCount(rangesForText(cell));
          if (lines > 1) add('numeric_cell_wrapped', table, {text, lines, cellBox: rect(cell)});
        }
        // Also check numbers embedded in labels/prose; wrapping at surrounding
        // spaces is fine, but splitting within a numeric token is not.
        const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) {
          if (node.parentElement.closest('td,th') !== cell || node.parentElement.closest('math,annotation,script,style')) continue;
          const pattern = /[+\-−]?\d+(?:[.,]\d+)*(?:[eE][+\-]?\d+)?(?:%|[KMB])?/g;
          let match;
          while ((match = pattern.exec(node.textContent))) {
            if (match[0].length < 2) continue;
            const range = document.createRange(); range.setStart(node, match.index);
            range.setEnd(node, match.index + match[0].length);
            const rectangles = [...range.getClientRects()];
            if (!rectangles.some(r => r.width && r.height)) continue;
            metrics.numericTokensChecked++;
            if (lineCount(rectangles) > 1) add('numeric_token_wrapped', table, {text: match[0], cellBox: rect(cell)});
          }
        }
      }
    }
  }
  for (const caption of article.querySelectorAll('figcaption')) {
    const figure = caption.closest('figure');
    if (!figure) continue;
    const localTables = [...figure.querySelectorAll('.ltx_tabular')]
      .filter(table => !table.parentElement.closest('.ltx_tabular') && !caption.contains(table));
    if (!localTables.length) continue;
    metrics.captionsChecked++;
    const cr = rect(caption);
    for (const table of localTables) {
      const tr = rect(table);
      if (Math.min(cr.right, tr.right) - Math.max(cr.left, tr.left) > 2 &&
          Math.min(cr.bottom, tr.bottom) - Math.max(cr.top, tr.top) > 2) {
        add('caption_overlaps_table', table, {caption: cr, tableBox: tr});
      }
    }
  }
  if (document.documentElement.scrollWidth > innerWidth + 2) {
    issues.push({kind: 'page_horizontal_overflow', viewport: innerWidth, scrollWidth: document.documentElement.scrollWidth});
  }
  return {issues, metrics};
}
