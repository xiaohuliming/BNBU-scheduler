/* A4 estimates from the school's public rate card; never a PaperCut quotation. */
'use strict';
(() => {
  const source = Object.freeze({
    url: 'https://itsc.bnbu.edu.cn/IT_Services/Printing.htm',
    updated: '2024-08-30',
    location: 'LRC / T6-103',
    currency: 'CNY',
    single: Object.freeze({ grayscale: 20, color: 180 }),
    duplex: Object.freeze({ grayscale: 36, color: 360 }),
  });
  const amount = cents => '￥' + (cents / 100).toFixed(2);
  function estimate(pages, options) {
    if (!Number.isInteger(pages) || pages < 1 || !Number.isInteger(options?.copies) || options.copies < 1 ||
        !Object.hasOwn(source.single, options.color) || !['one-sided', 'two-sided-long-edge', 'two-sided-short-edge'].includes(options.sides)) return null;
    const one = source.single[options.color], two = source.duplex[options.color];
    let min, max;
    if (options.sides === 'one-sided') {
      min = max = pages * one * options.copies;
    } else {
      const pairs = Math.floor(pages / 2), odd = pages % 2;
      // The published table does not define odd final sheets. Show the range
      // between per-face charging and charging each copy's entire final sheet.
      min = (pairs * two + odd * two / 2) * options.copies;
      max = (pairs + odd) * two * options.copies;
    }
    const notes = [];
    if (options.color === 'color') notes.push('彩色按全部彩页估算，混合黑白页的实际费用可能更低。');
    if (min !== max) notes.push('双面奇数页末张规则未公开，显示按面至按整张计费的范围。');
    notes.push('依据学校公开价目表，实际扣费以刷卡结算为准。');
    return Object.freeze({ min_cents: min, max_cents: max,
      display: min === max ? amount(min) : amount(min) + '–' + amount(max),
      ranged: min !== max, note: notes.join(' '), source });
  }
  window.MaxcoursePrintPricing = Object.freeze({ source, estimate });
})();
