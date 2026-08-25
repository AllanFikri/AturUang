/**
 * Money Tracks V12 — Core Pure Calculations (K6)
 *
 * Pure functions for derived client-side calculations.
 * Does NOT call `new Date()` internally — uses `ctx` as single source of truth.
 */

function getDaysInMonth(year, month) {
  var y = Number(year);
  var m = Number(month);
  if (m === 2) {
    var isLeap = (y % 4 === 0 && y % 100 !== 0) || (y % 400 === 0);
    return isLeap ? 29 : 28;
  }
  return [4, 6, 9, 11].indexOf(m) !== -1 ? 30 : 31;
}

function parseMonthString(monthStr) {
  var str = String(monthStr || '');
  var parts = str.split('-');
  var year = parseInt(parts[0], 10) || 2026;
  var month = parseInt(parts[1], 10) || 1;
  return { year: year, month: month };
}

function computeSafeDaily(availableForMonth, ctx) {
  var safeCtx = ctx || {};
  var parsed = parseMonthString(safeCtx.month);
  var daysInMonth = getDaysInMonth(parsed.year, parsed.month);
  var selMonth = safeCtx.month || '';
  var curMonth = safeCtx.currentMonth || selMonth;
  var todayStr = safeCtx.today || '';

  var daysLeft = 0;
  if (selMonth > curMonth) {
    // Future month: all days in month
    daysLeft = daysInMonth;
  } else if (selMonth < curMonth) {
    // Past month: 0 days left
    daysLeft = 0;
  } else {
    // Current month
    var refDay = todayStr ? parseInt(todayStr.slice(8, 10), 10) : 1;
    daysLeft = Math.max(1, daysInMonth - refDay + 1);
  }

  var safeDaily = daysLeft > 0 ? (availableForMonth / daysLeft) : 0;
  return { daysInMonth: daysInMonth, daysLeft: daysLeft, safeDaily: safeDaily };
}

function computeDailyAverage(spent, ctx) {
  var safeCtx = ctx || {};
  var parsed = parseMonthString(safeCtx.month);
  var daysInMonth = getDaysInMonth(parsed.year, parsed.month);
  var selMonth = safeCtx.month || '';
  var curMonth = safeCtx.currentMonth || selMonth;
  var todayStr = safeCtx.today || '';

  var dayOfMonth = 0;
  if (selMonth < curMonth) {
    // Past month: entire month elapsed
    dayOfMonth = daysInMonth;
  } else if (selMonth > curMonth) {
    // Future month: 0 days elapsed
    dayOfMonth = 0;
  } else {
    // Current month
    var refDay = todayStr ? parseInt(todayStr.slice(8, 10), 10) : 1;
    dayOfMonth = Math.max(1, Math.min(daysInMonth, refDay));
  }

  var dailyAvg = dayOfMonth > 0 ? (spent / dayOfMonth) : 0;
  return { dayOfMonth: dayOfMonth, dailyAvg: dailyAvg };
}

function componentsMatchHero(hero, components, tolerance) {
  if (tolerance === undefined) tolerance = 0.005;
  if (hero === null || hero === undefined || typeof hero !== 'number' || isNaN(hero) || !isFinite(hero)) {
    return false;
  }
  if (!components || typeof components !== 'object') {
    return false;
  }

  var sum = 0;
  if (Array.isArray(components)) {
    if (components.length === 0) return false;
    for (var i = 0; i < components.length; i++) {
      var val = components[i];
      if (val === null || val === undefined || typeof val !== 'number' || isNaN(val) || !isFinite(val)) {
        return false;
      }
      sum += val;
    }
  } else {
    var tb = Number(components.totalBalance != null ? components.totalBalance : 0);
    var em = Number(components.emergencyAllocated != null ? components.emergencyAllocated : 0);
    var ga = Number(components.goalsAllocated != null ? components.goalsAllocated : 0);
    var gen = Number(components.generalAllocated != null ? components.generalAllocated : 0);
    var ps = Number(components.protectedSavings != null ? components.protectedSavings : (em + ga + gen));
    var pt = Number(components.passThroughOutstanding != null ? components.passThroughOutstanding : 0);
    var ec = Number(components.effectiveConfirmedCommitments != null ? components.effectiveConfirmedCommitments : (components.currentCommitments != null ? components.currentCommitments : 0));
    var tr = Number(components.tentativeReserved != null ? components.tentativeReserved : 0);
    var pe = Number(components.pendingExpenses != null ? components.pendingExpenses : 0);

    if (components.emergencyAllocated !== undefined || components.goalsAllocated !== undefined) {
      sum = tb - em - ga - gen - ec - tr - pt - pe;
    } else {
      sum = tb - ps - pt - ec - pe;
    }
  }

  var hRounded = Math.round(Number(hero) * 100) / 100;
  var sumRounded = Math.round(Number(sum) * 100) / 100;
  var diff = Math.abs(hRounded - sumRounded);
  return diff < (tolerance + 1e-9);
}


function parseMoneyInput(val) {
  if (val === null || val === undefined || val === '') return 0;
  if (typeof val === 'number') return Math.round(val * 100) / 100;
  var s = String(val).trim();
  if (!s) return 0;

  var isNeg = s.indexOf('-') !== -1;
  s = s.replace(/[^0-9,.]/g, '');
  if (!s) return 0;

  if (s.indexOf(',') !== -1) {
    var parts = s.split(',');
    var intPart = parts[0].replace(/\./g, '');
    var decPart = parts.slice(1).join('').slice(0, 2);
    var num = Number(intPart + (decPart ? '.' + decPart : ''));
    var res = isNaN(num) ? 0 : (isNeg ? -num : num);
    return Math.round(res * 100) / 100;
  }

  var dotCount = (s.match(/\./g) || []).length;
  if (dotCount > 1) {
    var num2 = Number(s.replace(/\./g, ''));
    var res2 = isNaN(num2) ? 0 : (isNeg ? -num2 : num2);
    return Math.round(res2 * 100) / 100;
  }

  if (dotCount === 1) {
    var p = s.split('.');
    if (p[1].length === 3 && p[1].indexOf(',') === -1) {
      var num3 = Number(s.replace(/\./g, ''));
      var res3 = isNaN(num3) ? 0 : (isNeg ? -num3 : num3);
      return Math.round(res3 * 100) / 100;
    } else {
      var num4 = Number(s);
      var res4 = isNaN(num4) ? 0 : (isNeg ? -num4 : num4);
      return Math.round(res4 * 100) / 100;
    }
  }

  var num5 = Number(s);
  var res5 = isNaN(num5) ? 0 : (isNeg ? -num5 : num5);
  return Math.round(res5 * 100) / 100;
}

function formatMoneyInput(val, allowDecimals) {
  if (allowDecimals === undefined) allowDecimals = true;
  if (val === null || val === undefined || val === '') return '';
  var num = parseMoneyInput(val);
  if (isNaN(num)) return '';

  var isNeg = num < 0;
  var absNum = Math.abs(num);
  var intPart = Math.floor(absNum);
  var decPart = Math.round((absNum - intPart) * 100);

  var formattedInt = intPart.toLocaleString('id-ID');
  if (allowDecimals && decPart > 0) {
    var decStr = decPart < 10 ? '0' + decPart : String(decPart);
    return (isNeg ? '-' : '') + formattedInt + ',' + decStr;
  }
  return (isNeg ? '-' : '') + formattedInt;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    getDaysInMonth: getDaysInMonth,
    parseMonthString: parseMonthString,
    computeSafeDaily: computeSafeDaily,
    computeDailyAverage: computeDailyAverage,
    componentsMatchHero: componentsMatchHero,
    parseMoneyInput: parseMoneyInput,
    formatMoneyInput: formatMoneyInput,
  };
}
