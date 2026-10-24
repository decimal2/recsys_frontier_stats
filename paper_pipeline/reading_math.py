"""Measure inline MathML in the browser and scroll only oversized formulas.

Ordinary formulas retain their native inline layout and baseline. No TeX,
MathML, equation identifiers, or surrounding prose are rewritten.
"""


MATH_LAYOUT_STYLES = """
.paper-fulltext figure.ltx_minipage,.paper-fulltext .ltx_listing{max-width:100%;box-sizing:border-box}
.paper-fulltext .tex-diagram-scroll{display:block;max-width:100%;overflow:auto;text-align:start;padding:2px 0}
.paper-fulltext svg.ltx_picture{background:#fff;color:#111;line-height:normal;overflow-wrap:normal;word-break:normal}
.paper-fulltext svg.ltx_picture foreignObject{white-space:nowrap;line-height:normal;text-align:start}
.paper-fulltext svg.ltx_picture foreignObject .ltx_minipage{white-space:normal}
.paper-fulltext svg.ltx_picture foreignObject .ltx_p{margin:0;line-height:normal}
"""


MATH_LAYOUT_SCRIPT = r"""
(() => {
  function initializeReadingMath() {
    const article = document.querySelector('article.paper-fulltext');
    if (!article || article.dataset.readingMathInitialized) return;
    article.dataset.readingMathInitialized = 'true';
    const formulas = [...article.querySelectorAll('math:not([display="block"])')]
      .filter(math => !math.closest('table, .ltx_tabular, .ltx_equation, .ltx_listing, pre, code, svg'));
    // SVGs without a viewBox cannot safely be resized by CSS. Scroll the whole
    // drawing instead, preserving its coordinates, foreignObjects and MathML.
    const diagrams = [...article.querySelectorAll('svg.ltx_picture')]
      .filter(svg => !svg.parentElement?.closest('svg'));
    let queued = false;

    function update() {
      queued = false;
      for (const math of formulas) {
        const paragraph = math.closest('.ltx_p, .paper-paragraph, p, li') || article;
        const style = getComputedStyle(paragraph);
        const available = paragraph.clientWidth
          - (parseFloat(style.paddingLeft) || 0) - (parseFloat(style.paddingRight) || 0);
        if (available <= 0) continue;
        const wrapper = math.parentElement?.matches('[data-reading-math-scroll]')
          ? math.parentElement : null;
        const tooWide = math.getBoundingClientRect().width > available + 2;
        if (tooWide && !wrapper) {
          const scroller = document.createElement('span');
          scroller.className = 'tex-inline-math-scroll';
          scroller.dataset.readingMathScroll = 'true';
          scroller.tabIndex = 0;
          scroller.setAttribute('role', 'group');
          scroller.setAttribute('aria-label', '公式可横向滚动');
          math.before(scroller);
          scroller.append(math);
        } else if (!tooWide && wrapper) {
          wrapper.replaceWith(...wrapper.childNodes);
        }
      }
      for (const svg of diagrams) {
        const wrapper = svg.parentElement?.matches('[data-reading-diagram-scroll]')
          ? svg.parentElement : null;
        let protectedByScroller = false;
        for (let p = wrapper?.parentElement || svg.parentElement;
             p && p !== article.parentElement; p = p.parentElement) {
          if (['auto', 'scroll'].includes(getComputedStyle(p).overflowX)) {
            protectedByScroller = true;
            break;
          }
        }
        if (protectedByScroller) continue;
        const paragraph = svg.closest('.ltx_para, .ltx_p, .paper-paragraph, p, li') || article;
        const style = getComputedStyle(paragraph);
        const available = paragraph.clientWidth
          - (parseFloat(style.paddingLeft) || 0) - (parseFloat(style.paddingRight) || 0);
        if (available <= 0) continue;
        const tooWide = svg.getBoundingClientRect().width > available + 2;
        if (tooWide && !wrapper) {
          const scroller = document.createElement('span');
          scroller.className = 'tex-diagram-scroll';
          scroller.dataset.readingDiagramScroll = 'true';
          scroller.tabIndex = 0;
          scroller.setAttribute('role', 'group');
          scroller.setAttribute('aria-label', '图示可横向滚动');
          svg.before(scroller);
          scroller.append(svg);
        } else if (!tooWide && wrapper) {
          wrapper.replaceWith(...wrapper.childNodes);
        }
      }
    }

    function schedule() {
      if (!queued) {
        queued = true;
        requestAnimationFrame(update);
      }
    }

    let previousWidth = article.getBoundingClientRect().width;
    if ('ResizeObserver' in window) {
      new ResizeObserver(() => {
        const width = article.getBoundingClientRect().width;
        if (Math.abs(width - previousWidth) > 0.5) {
          previousWidth = width;
          schedule();
        }
      }).observe(article);
    }
    window.addEventListener('resize', schedule, {passive: true});
    if (document.fonts) {
      document.fonts.ready.then(schedule);
      document.fonts.addEventListener?.('loadingdone', schedule);
    }
    schedule();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeReadingMath, {once: true});
  } else {
    initializeReadingMath();
  }
})();
"""
