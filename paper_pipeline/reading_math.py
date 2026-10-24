"""Measure inline MathML in the browser and scroll only oversized formulas.

Ordinary formulas retain their native inline layout and baseline. No TeX,
MathML, equation identifiers, or surrounding prose are rewritten.
"""


MATH_LAYOUT_SCRIPT = r"""
(() => {
  function initializeReadingMath() {
    const article = document.querySelector('article.paper-fulltext');
    if (!article || article.dataset.readingMathInitialized) return;
    article.dataset.readingMathInitialized = 'true';
    const formulas = [...article.querySelectorAll('math:not([display="block"])')]
      .filter(math => !math.closest('table, .ltx_tabular, .ltx_equation, .ltx_listing, pre, code, svg'));
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
