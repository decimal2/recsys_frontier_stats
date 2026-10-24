// Import these pure DOM functions into the browser audit, or pass them to
// Playwright page.evaluate. No network or screenshot services are needed.
export function captureMathSource() {
  return [...document.querySelectorAll('article.paper-fulltext math')].map(m => ({id:m.id, html:m.outerHTML}));
}

export function captureDiagramSource() {
  return [...document.querySelectorAll('article.paper-fulltext svg.ltx_picture')]
    .filter(svg=>!svg.parentElement?.closest('svg'))
    .map(svg=>({id:svg.id,html:svg.outerHTML}));
}

export function collectMathEvidence() {
  const article=document.querySelector('article.paper-fulltext');
  if(!article)throw Error('Missing paper-fulltext article');
  const rect=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom};};
  const wrappers=[...article.querySelectorAll('[data-reading-math-scroll],[data-reading-diagram-scroll]')].map(e=>({math:e.querySelector('math')?.id,diagram:e.querySelector('svg')?.id,rect:rect(e),clientWidth:e.clientWidth,scrollWidth:e.scrollWidth,overflowX:getComputedStyle(e).overflowX,tabIndex:e.tabIndex}));
  const protection=e=>{
    let clipping=null,scrolling=null;
    const er=rect(e);
    for(let p=e.parentElement;p&&p!==article.parentElement;p=p.parentElement){
      const style=getComputedStyle(p),r=rect(p);
      if(['auto','scroll'].includes(style.overflowX)&&p.scrollWidth>p.clientWidth+2){
        scrolling={id:p.id,class:p.getAttribute('class'),rect:r,clientWidth:p.clientWidth,scrollWidth:p.scrollWidth,scrollLeft:p.scrollLeft};
        // Content before a scroller's origin cannot be reached by scrolling.
        if(er.x<r.x+p.clientLeft-p.scrollLeft-3)clipping={id:p.id,class:p.getAttribute('class'),overflow:'unreachable before scroll origin'};
        break;
      }
      if(['hidden','clip'].includes(style.overflowX)&&(er.right>r.right+2||er.x<r.x-2)){
        clipping={id:p.id,class:p.getAttribute('class'),overflow:style.overflowX};break;
      }
    }
    return {clipping,scrolling};
  };
  const math=[...article.querySelectorAll('math')].map(m=>{
    const diagram=m.closest('svg'),listing=m.closest('.ltx_listing,pre,code');
    const inTable=!!m.closest('table,.ltx_tabular,.ltx_equation');
    // SVG foreignObject spans often have clientWidth=0; their coordinates are
    // drawing coordinates, not the width available to ordinary prose.
    const context=diagram?'diagram':inTable?'table':listing?'listing':'prose';
    const paragraph=listing||m.closest('.ltx_p,.paper-paragraph,p,li')||article;
    const pStyle=getComputedStyle(paragraph),pRect=rect(paragraph);
    const available=paragraph.clientWidth-(parseFloat(pStyle.paddingLeft)||0)-(parseFloat(pStyle.paddingRight)||0);
    return {id:m.id,display:m.getAttribute('display')||'inline',rect:rect(m),paragraph:pRect,available,context,diagram:diagram?.id,wrapped:!!m.closest('[data-reading-math-scroll]'),inTable,...protection(m)};
  });
  const regions=[...article.querySelectorAll('svg.ltx_picture,.ltx_listing')]
    .filter(e=>!e.parentElement?.closest('svg'))
    .map(e=>({id:e.id,kind:e.tagName.toLowerCase()==='svg'?'diagram':'listing',rect:rect(e),article:rect(article),...protection(e)}));
  return {viewport:innerWidth,documentWidth:document.documentElement.scrollWidth,mathCount:math.length,wrappers,math,regions,justifiedParagraphs:[...article.querySelectorAll('.ltx_p,.paper-paragraph')].filter(p=>getComputedStyle(p).textAlign==='justify').length};
}

export function assertMathEvidence(report) {
  const errors=[];
  for(const m of report.math){
    if(m.clipping)errors.push({id:m.id,problem:'formula is clipped by non-scrollable ancestor',ancestor:m.clipping});
    if(m.context==='prose'&&m.display==='inline'){
      if(m.rect.width>m.available+2&&!m.wrapped)errors.push({id:m.id,problem:'oversized inline formula has no scroll container',width:m.rect.width,available:m.available});
      if(m.wrapped&&m.rect.width<=m.available+2)errors.push({id:m.id,problem:'fitting inline formula was unnecessarily wrapped'});
      if(!m.wrapped&&(m.rect.right>m.paragraph.right+3||m.rect.x<m.paragraph.x-3))errors.push({id:m.id,problem:'unwrapped inline formula escapes paragraph'});
    }
    if(m.context==='listing'&&!m.scrolling&&(m.rect.right>m.paragraph.right+3||m.rect.x<m.paragraph.x-3))errors.push({id:m.id,problem:'algorithm formula escapes without a scroll container'});
  }
  for(const r of report.regions||[]){
    if(r.clipping)errors.push({id:r.id,problem:`${r.kind} is clipped`,ancestor:r.clipping});
    if(!r.scrolling&&(r.rect.right>r.article.right+3||r.rect.x<r.article.x-3))errors.push({id:r.id,problem:`${r.kind} escapes the reading area without a scroll container`});
  }
  for(const w of report.wrappers){
    if(!['auto','scroll'].includes(w.overflowX))errors.push({id:w.diagram||w.math,problem:'wrapper does not allow horizontal scrolling'});
    if(w.scrollWidth<=w.clientWidth+2)errors.push({id:w.diagram||w.math,problem:'wrapper added without actual overflow'});
    if(w.tabIndex<0)errors.push({id:w.diagram||w.math,problem:'overflow not keyboard focusable'});
  }
  if(report.justifiedParagraphs)errors.push({problem:'Chinese paper paragraphs still use justification',count:report.justifiedParagraphs});
  return errors;
}

// Wide glyphs with fewer than 28 visible characters exercise the measurement
// criterion, and short-symbol geometry must remain exactly native.
export const regressionFixture=`<!doctype html><meta charset="utf-8"><style>
body{margin:20px}article{max-width:none}p{margin:16px 0;text-align:left;font:18px/1.8 sans-serif}
math{font-family:math}#wide{font-size:40px}
.tex-inline-math-scroll{display:block;max-width:100%;overflow-x:auto;margin:.35em 0}
</style><article class="paper-fulltext">
<p id="baseline">短公式 <math id="short"><msup><mi>x</mi><mn>2</mn></msup></math> 保持原生基线。</p>
<p>窄屏下需要滚动 <math id="wide"><semantics><mi>WWWWWWWWWWWWWWWWWW</mi><annotation encoding="application/x-tex">WWWWWWWWWWWWWWWWWW</annotation></semantics></math> 但宽屏应恢复行内。</p>
</article>`;

// No viewBox is intentional. Resizing only the SVG viewport would crop its
// drawing, while adding a viewBox would rewrite source graphics coordinates.
export const diagramRegressionFixture=`<!doctype html><meta charset="utf-8"><style>
body{margin:20px}p{font:18px/1.8 sans-serif}article{max-width:none}
.tex-diagram-scroll{display:block;max-width:100%;overflow:auto;text-align:start;padding:2px 0}
</style><article class="paper-fulltext">
<p id="icon-baseline">小图标 <svg id="short-diagram" class="ltx_picture" width="24" height="24"><circle cx="12" cy="12" r="10" fill="blue"/></svg> 保持行内。</p>
<div class="ltx_para"><svg id="wide-diagram" class="ltx_picture" width="760" height="80" overflow="visible"><path d="M 0 40 L 750 40" stroke="black"/><foreignObject x="20" y="10" width="700" height="50"><span xmlns="http://www.w3.org/1999/xhtml">图内公式 <math id="diagram-math"><msup><mi>x</mi><mn>2</mn></msup></math></span></foreignObject></svg></div>
</article>`;

// Runner requirements:
// 1. Load fixture at 390px, await fonts.ready, snapshot every math.outerHTML and
//    #short.getBoundingClientRect() before injecting MATH_LAYOUT_SCRIPT.
// 2. Inject script, wait two animation frames; assert only #wide is wrapped,
//    no math.outerHTML changes, and all short formula x/y/width/height unchanged.
// 3. Resize to 1440px, wait two frames; assert no wrappers and same MathML.
// 4. Resize back to 390px; assert one wrapper and no nested wrappers.
// 5. On all 38 real reading pages at 1440 and 390px, run collectMathEvidence /
//    assertMathEvidence, compare mathematical snapshots before/after, save JSON.
// 6. Visually inspect LETTER #S7.p7 (user's screenshot), GRPO #S4.E3,
//    GSPO #S4.SS1, OPSD #S3.E6 and #S3.E9, both widths and light/dark as needed.
