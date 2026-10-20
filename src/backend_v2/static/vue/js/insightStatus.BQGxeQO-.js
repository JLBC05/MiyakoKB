function t(l){const e=l.currentTask?.status;return e==="completed"?l.fullyAnalyzed?"completed":"idle":e==="cancelled"?"cancelled":e||(l.fullyAnalyzed?"completed":"idle")}export{t as r};
