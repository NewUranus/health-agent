/** 四舍五入到 1 位小数，加 EPSILON 避免 13.45 被算成 13.4。 */
export const formatPercent = (value: number) => `${(Math.round((value + Number.EPSILON) * 10) / 10).toFixed(1)}%`;
export const formatPoints = (value: number) => formatPercent(value).slice(0, -1);
