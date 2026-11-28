#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define STRESS_VM_BUFFER 5000.0
#define UNCERTAINTY_BUFFER 5000.0

#ifdef MOEX_FASTCORE_SANITIZE
#define FASTCORE_MALLOC(sz) malloc(sz)
#define FASTCORE_FREE(ptr) free(ptr)
#else
#define FASTCORE_MALLOC(sz) PyMem_Malloc(sz)
#define FASTCORE_FREE(ptr) PyMem_Free(ptr)
#endif

static PyObject *k_date = NULL;
static PyObject *k_cumulative_net_rub = NULL;
static PyObject *k_account_value_rub = NULL;
static PyObject *k_cash_shortfall = NULL;
static PyObject *k_funding_rub = NULL;
static PyObject *k_funding_contracts = NULL;
static PyObject *s_round_method = NULL;
static PyObject *py_two = NULL;

static int ensure_interned_keys(void) {
    if (k_date != NULL) return 0;
    k_date = PyUnicode_InternFromString("date");
    k_cumulative_net_rub = PyUnicode_InternFromString("cumulative_net_rub");
    k_account_value_rub = PyUnicode_InternFromString("account_value_rub");
    k_cash_shortfall = PyUnicode_InternFromString("cash_shortfall");
    k_funding_rub = PyUnicode_InternFromString("funding_rub");
    k_funding_contracts = PyUnicode_InternFromString("funding_contracts");
    s_round_method = PyUnicode_InternFromString("__round__");
    py_two = PyLong_FromLong(2);
    if (!k_date || !k_cumulative_net_rub || !k_account_value_rub || !k_cash_shortfall ||
        !k_funding_rub || !k_funding_contracts || !s_round_method || !py_two) {
        return -1;
    }
    return 0;
}

static int exact_round2(double x, double *out) {
    if (isfinite(x) && fabs(x) < 1e13) {
        double scaled = x * 100.0;
        double fl = floor(scaled);
        double diff = fabs((scaled - fl) - 0.5);
        if (diff > 1e-6) {
            double r = floor(scaled + 0.5);
            *out = (r == 0.0) ? copysign(0.0, x) : (r / 100.0);
            return 0;
        }
    }
    if (!s_round_method || !py_two) {
        if (ensure_interned_keys() < 0) return -1;
    }
    PyObject *f = PyFloat_FromDouble(x);
    if (!f) return -1;
    PyObject *res = PyObject_CallMethodOneArg(f, s_round_method, py_two);
    Py_DECREF(f);
    if (!res) return -1;
    *out = PyFloat_AsDouble(res);
    Py_DECREF(res);
    if (*out == -1.0 && PyErr_Occurred()) return -1;
    return 0;
}

/* ---------------- Double-Double (106-bit) exact helpers ---------------- */

typedef struct {
    double hi;
    double lo;
} dd_t;

static inline dd_t dd_two_sum(double a, double b) {
    double s = a + b;
    double bb = s - a;
    double err = (a - (s - bb)) + (b - bb);
    dd_t r = {s, err};
    return r;
}

static inline dd_t dd_quick_two_sum(double a, double b) {
    double s = a + b;
    double err = b - (s - a);
    dd_t r = {s, err};
    return r;
}

static inline dd_t dd_add_d(dd_t a, double b) {
    dd_t s = dd_two_sum(a.hi, b);
    s.lo += a.lo;
    return dd_quick_two_sum(s.hi, s.lo);
}

static inline dd_t dd_add(dd_t a, dd_t b) {
    dd_t s = dd_two_sum(a.hi, b.hi);
    dd_t t = dd_two_sum(a.lo, b.lo);
    s.lo += t.hi;
    s = dd_quick_two_sum(s.hi, s.lo);
    s.lo += t.lo;
    return dd_quick_two_sum(s.hi, s.lo);
}

static inline dd_t dd_sub_d(double a, dd_t b) {
    dd_t s = dd_two_sum(a, -b.hi);
    s.lo -= b.lo;
    return dd_quick_two_sum(s.hi, s.lo);
}

static inline dd_t dd_two_prod(double a, double b) {
    double p = a * b;
    double err = fma(a, b, -p);
    dd_t r = {p, err};
    return r;
}

static inline dd_t dd_sqr(dd_t a) {
    dd_t p = dd_two_prod(a.hi, a.hi);
    p.lo += 2.0 * a.hi * a.lo + a.lo * a.lo;
    return dd_quick_two_sum(p.hi, p.lo);
}

static inline dd_t dd_div_d(dd_t a, double b) {
    double q1 = a.hi / b;
    dd_t p = dd_two_prod(q1, b);
    dd_t r = dd_two_sum(a.hi, -p.hi);
    r.lo = (r.lo + a.lo) - p.lo;
    double q2 = (r.hi + r.lo) / b;
    return dd_quick_two_sum(q1, q2);
}

typedef struct {
    dd_t *suf;
    dd_t pref;
    Py_ssize_t block, pref_end;
} SlidingSum;

/* Preserve accumulation order, including windows skipped by the calendar scan. */
static inline dd_t sliding_sum(SlidingSum *state, const double *x, Py_ssize_t window,
                               Py_ssize_t start, Py_ssize_t end) {
    Py_ssize_t block = end / window, anchor = block * window;
    if (block != state->block) {
        state->block = block;
        state->suf[0] = (dd_t){0.0, 0.0};
        Py_ssize_t lim = (anchor < window) ? anchor : window;
        for (Py_ssize_t k = 1; k <= lim; k++)
            state->suf[k] = dd_add_d(state->suf[k - 1], x[anchor - k]);
        state->pref = (dd_t){0.0, 0.0};
        state->pref_end = anchor;
    }
    while (state->pref_end < end) {
        state->pref = dd_add_d(state->pref, x[state->pref_end]);
        state->pref_end++;
    }
    return state->suf[anchor - start];
}

/* ---------------- Buffer / sequence extraction helpers ---------------- */

typedef struct {
    Py_buffer view;
    void *owned;
    int has_view;
} BufArg;

typedef enum { BUF_DOUBLE, BUF_BYTE, BUF_INT32 } BufKind;

static void buf_release_all(BufArg *buffers, size_t count) {
    for (size_t i = 0; i < count; i++) {
        if (buffers[i].has_view) PyBuffer_Release(&buffers[i].view);
        FASTCORE_FREE(buffers[i].owned);
    }
}

static int buf_set(PyObject *item, BufArg *arg, BufKind kind, Py_ssize_t i) {
    if (kind == BUF_DOUBLE) {
        double v = PyFloat_CheckExact(item) ? PyFloat_AS_DOUBLE(item) : PyFloat_AsDouble(item);
        if (v == -1.0 && PyErr_Occurred()) return -1;
        ((double *)arg->owned)[i] = v;
    } else if (kind == BUF_BYTE) {
        int v = PyObject_IsTrue(item);
        if (v < 0) return -1;
        ((unsigned char *)arg->owned)[i] = (unsigned char)v;
    } else {
        long v = PyLong_AsLong(item);
        if (v == -1 && PyErr_Occurred()) return -1;
        ((int32_t *)arg->owned)[i] = (int32_t)v;
    }
    return 0;
}

static int buf_get(PyObject *obj, Py_ssize_t n, BufArg *arg, BufKind kind) {
    const size_t sizes[] = {sizeof(double), sizeof(unsigned char), sizeof(int32_t)};
    const char *names[] = {"double", "byte", "int32"};
    size_t size = sizes[kind];
    if (PyObject_CheckBuffer(obj)) {
        if (PyObject_GetBuffer(obj, &arg->view, PyBUF_SIMPLE) == 0) {
            if (arg->view.len == n * (Py_ssize_t)size) {
                arg->has_view = 1;
                return 0;
            }
            PyBuffer_Release(&arg->view);
        } else {
            PyErr_Clear();
        }
    }
    if (!PyList_Check(obj) || PyList_GET_SIZE(obj) != n) {
        PyErr_Format(PyExc_TypeError, "expected %s buffer or list of matching length", names[kind]);
        return -1;
    }
    arg->owned = FASTCORE_MALLOC((size_t)(n > 0 ? n : 1) * size);
    if (!arg->owned) { PyErr_NoMemory(); return -1; }
    for (Py_ssize_t i = 0; i < n; i++)
        if (buf_set(PyList_GET_ITEM(obj, i), arg, kind, i) < 0) return -1;
    return 0;
}

static void *buf_data(BufArg *arg) {
    return arg->has_view ? arg->view.buf : arg->owned;
}

/* ---------------- 1. Population rolling z-score  ----------------
 * Uses Van Herk / Gil-Werman O(1) sliding window double-double sum for the mean
 * (zero subtraction, O(1) per bar regardless of lookback) + branchless positive
 * Neumaier variance pass + zero-copy Py_buffer support.
 */
// NOLINTNEXTLINE(readability-function-cognitive-complexity): baselined legacy C99 hot loop
static PyObject *fast_compute_rolling_z(PyObject *Py_UNUSED(self), PyObject *args) {
    PyObject *spread_obj;
    Py_ssize_t lookback, delay;
    if (!PyArg_ParseTuple(args, "Onn", &spread_obj, &lookback, &delay)) {
        return NULL;
    }
    if (lookback <= 0 || delay < 0) {
        PyErr_SetString(PyExc_ValueError, "invalid lookback or delay");
        return NULL;
    }
    Py_ssize_t n = PyObject_Size(spread_obj);
    if (n < 0) return NULL;
    PyObject *result = PyList_New(n);
    if (!result) return NULL;
    if (n == 0) return result;

    BufArg buffers[1] = {0};
    const double *x = NULL;
    if (buf_get(spread_obj, n, &buffers[0], BUF_DOUBLE) < 0) {
        buf_release_all(buffers, 1);
        Py_DECREF(result);
        return NULL;
    }
    x = buf_data(&buffers[0]);

    Py_ssize_t start = lookback + delay;
    Py_ssize_t pre = start < n ? start : n;
    for (Py_ssize_t i = 0; i < pre; i++) {
        Py_INCREF(Py_None);
        PyList_SET_ITEM(result, i, Py_None);
    }
    if (start < n) {
        double lb = (double)lookback;
        dd_t suf_stack[256];
        dd_t *suf = (lookback + 1 <= 256)
                        ? suf_stack
                        : (dd_t *)FASTCORE_MALLOC((size_t)(lookback + 1) * sizeof(dd_t));
        if (!suf) {
            buf_release_all(buffers, 1);
            Py_DECREF(result);
            return PyErr_NoMemory();
        }
        SlidingSum sums = {suf, {0.0, 0.0}, -1, 0};

        for (Py_ssize_t index = start; index < n; index++) {
            Py_ssize_t signal_index = index - delay;
            Py_ssize_t w0 = signal_index - lookback;
            dd_t s1 = sliding_sum(&sums, x, lookback, w0, signal_index);
            dd_t tot = dd_two_sum(s1.hi, sums.pref.hi);
            tot.lo += s1.lo + sums.pref.lo;
            double mean = (tot.hi + tot.lo) / lb;

            /* sq >= 0 and v_hi >= 0 so fabs() is redundant */
            double d0 = x[w0] - mean;
            double v_hi = d0 * d0;
            double v_lo = 0.0;
            for (Py_ssize_t j = w0 + 1; j < signal_index; j++) {
                double d = x[j] - mean;
                double sq = d * d;
                double t = v_hi + sq;
                v_lo += (v_hi >= sq) ? ((v_hi - t) + sq) : ((sq - t) + v_hi);
                v_hi = t;
            }
            double std = sqrt((v_hi + v_lo) / lb);
            double z = (std > 0.0) ? ((x[signal_index] - mean) / std) : 0.0;
            PyObject *zf = PyFloat_FromDouble(z);
            if (!zf) {
                if (suf != suf_stack) FASTCORE_FREE(suf);
                buf_release_all(buffers, 1);
                Py_DECREF(result);
                return NULL;
            }
            PyList_SET_ITEM(result, index, zf);
        }
        if (suf != suf_stack) FASTCORE_FREE(suf);
    }

    buf_release_all(buffers, 1);
    return result;
}

/* ---------------- 2. Calendar sample rolling z-score  ----------------
 * Matches Python `statistics.mean` and `statistics.stdev` to 0 ULP via 106-bit
 * double-double mean + 106-bit sum of squared deviations + Newton-refined sqrt.
 * Returns Python `array('d')` directly for zero-copy reuse in `calendar_zscore_scan`.
 */
// NOLINTNEXTLINE(readability-function-cognitive-complexity,readability-function-size): baselined legacy C99 hot loop
static PyObject *fast_calendar_sample_z(PyObject *Py_UNUSED(self), PyObject *args) {
    PyObject *spread_obj, *elig_obj, *elig_pref_obj;
    Py_ssize_t window;
    int include_current;
    if (!PyArg_ParseTuple(args, "OOOnp", &spread_obj, &elig_obj, &elig_pref_obj, &window, &include_current)) {
        return NULL;
    }
    if (window < 2) {
        PyErr_SetString(PyExc_ValueError, "sample z-score window must be >= 2");
        return NULL;
    }
    Py_ssize_t n = PyObject_Size(spread_obj);
    if (n < 0) return NULL;

    /* Allocate array.array('d', [nan]*n) via Python's array module or return bytes/array */
    static PyObject *array_cls = NULL;
    if (array_cls == NULL) {
        PyObject *arr_mod = PyImport_ImportModule("array");
        if (!arr_mod) return NULL;
        array_cls = PyObject_GetAttrString(arr_mod, "array");
        Py_DECREF(arr_mod);
        if (!array_cls) return NULL;
    }
    PyObject *empty_bytes = PyBytes_FromStringAndSize(NULL, n * (Py_ssize_t)sizeof(double));
    if (!empty_bytes) return NULL;
    char *z_raw = PyBytes_AS_STRING(empty_bytes);
    for (Py_ssize_t i = 0; i < n; i++) {
        memcpy(z_raw + (size_t)i * sizeof(double), &(const double){NAN}, sizeof(double));
    }
    PyObject *out_arr = PyObject_CallFunction(array_cls, "sO", "d", empty_bytes);
    Py_DECREF(empty_bytes);
    if (!out_arr) return NULL;
    if (n == 0) return out_arr;

    Py_buffer out_view;
    if (PyObject_GetBuffer(out_arr, &out_view, PyBUF_WRITABLE) < 0) {
        Py_DECREF(out_arr);
        return NULL;
    }
    double *z_out = (double *)out_view.buf;

    BufArg buffers[3] = {0};
    const double *spreads = NULL;
    const unsigned char *elig = NULL;
    const int32_t *elig_pref = NULL;

    if (buf_get(spread_obj, n, &buffers[0], BUF_DOUBLE) < 0 ||
        buf_get(elig_obj, n, &buffers[1], BUF_BYTE) < 0 ||
        buf_get(elig_pref_obj, n + 1, &buffers[2], BUF_INT32) < 0) {
        buf_release_all(buffers, 3);
        PyBuffer_Release(&out_view);
        Py_DECREF(out_arr);
        return NULL;
    }
    spreads = buf_data(&buffers[0]);
    elig = buf_data(&buffers[1]);
    elig_pref = buf_data(&buffers[2]);

    Py_ssize_t inc = include_current ? 1 : 0;
    Py_ssize_t first_idx = window - inc;
    if (first_idx < 0) first_idx = 0;
    double win_d = (double)window;
    double win_m1 = (double)(window - 1);

    /* Van Herk O(1) sliding window double-double sum for sample mean */
    dd_t suf_stack[256];
    dd_t *suf = (window + 1 <= 256)
                    ? suf_stack
                    : (dd_t *)FASTCORE_MALLOC((size_t)(window + 1) * sizeof(dd_t));
    if (!suf) {
        buf_release_all(buffers, 3);
        PyBuffer_Release(&out_view);
        Py_DECREF(out_arr);
        return PyErr_NoMemory();
    }
    SlidingSum sums = {suf, {0.0, 0.0}, -1, 0};

    for (Py_ssize_t index = first_idx; index < n; index++) {
        Py_ssize_t start = include_current ? (index - window + 1) : (index - window);
        Py_ssize_t end = include_current ? (index + 1) : index;
        if (!elig[index] || (Py_ssize_t)(elig_pref[end] - elig_pref[start]) != window) {
            continue;
        }
        dd_t s1 = sliding_sum(&sums, spreads, window, start, end);
        dd_t sum = dd_add(s1, sums.pref);
        dd_t mean = dd_div_d(sum, win_d);
        double mean_rounded = mean.hi + mean.lo;

        dd_t ss = {0.0, 0.0};
        for (Py_ssize_t j = start; j < end; j++) {
            dd_t d = dd_sub_d(spreads[j], mean);
            ss = dd_add(ss, dd_sqr(d));
        }
        dd_t var = dd_div_d(ss, win_m1);
        double v_hi = var.hi + var.lo;
        if (v_hi > 0.0) {
            double y = sqrt(v_hi);
            dd_t y2 = dd_two_prod(y, y);
            double rem = (var.hi - y2.hi) + (var.lo - y2.lo);
            double sd = y + rem / (2.0 * y);
            z_out[index] = (sd > 0.0) ? ((spreads[index] - mean_rounded) / sd) : NAN;
        } else {
            z_out[index] = NAN;
        }
    }

    if (suf != suf_stack) FASTCORE_FREE(suf);
    buf_release_all(buffers, 3);
    PyBuffer_Release(&out_view);
    return out_arr;
}

static int append_trade(PyObject *trades, PyObject *tuple) {
    if (!tuple) return -1;
    int result = PyList_Append(trades, tuple);
    Py_DECREF(tuple);
    return result;
}

/* ---------------- 3. Calendar z-score & convergence trade scans ----------------
 * Reason codes for calendar_zscore_scan:
 *   0 = z_reverted, 1 = max_hold, 2 = near_expiry_buffer, 3 = end_of_window
 * Reason codes for calendar_convergence_scan:
 *   2 = near_expiry_buffer, 3 = end_of_window
 * Returns list of (entry_idx, exit_idx, direction, reason_code, signal_idx, signal_val).
 */
// NOLINTNEXTLINE(readability-function-cognitive-complexity): baselined legacy C99 hot loop
static PyObject *fast_calendar_zscore_scan(PyObject *Py_UNUSED(self), PyObject *args) {
    PyObject *z_obj, *ts_obj, *dte_obj, *sig_el_obj, *ent_el_obj, *ext_el_obj;
    Py_ssize_t delay;
    double entry_z, exit_z, max_hold_days;
    long close_before;
    int close_at_end;

    if (!PyArg_ParseTuple(
            args, "OOOOOOndddlp",
            &z_obj, &ts_obj, &dte_obj, &sig_el_obj, &ent_el_obj, &ext_el_obj,
            &delay, &entry_z, &exit_z, &max_hold_days, &close_before, &close_at_end)) {
        return NULL;
    }
    Py_ssize_t n = PyObject_Size(z_obj);
    if (n < 0) return NULL;
    PyObject *trades = PyList_New(0);
    if (!trades || n == 0) return trades;

    BufArg buffers[6] = {0};
    const double *z = NULL, *ts_days = NULL;
    const int32_t *dte = NULL;
    const unsigned char *sig_el = NULL, *ent_el = NULL, *ext_el = NULL;

    if (buf_get(z_obj, n, &buffers[0], BUF_DOUBLE) < 0 ||
        buf_get(ts_obj, n, &buffers[1], BUF_DOUBLE) < 0 ||
        buf_get(dte_obj, n, &buffers[2], BUF_INT32) < 0 ||
        buf_get(sig_el_obj, n, &buffers[3], BUF_BYTE) < 0 ||
        buf_get(ent_el_obj, n, &buffers[4], BUF_BYTE) < 0 ||
        buf_get(ext_el_obj, n, &buffers[5], BUF_BYTE) < 0) {
        goto fail;
    }
    z = buf_data(&buffers[0]);
    ts_days = buf_data(&buffers[1]);
    dte = buf_data(&buffers[2]);
    sig_el = buf_data(&buffers[3]);
    ent_el = buf_data(&buffers[4]);
    ext_el = buf_data(&buffers[5]);

    int position = 0;
    Py_ssize_t entry_idx = 0, signal_idx = 0;
    double entry_ts = 0.0;

    for (Py_ssize_t index = 0; index < n; index++) {
        if (position != 0 && ext_el[index]) {
            double exit_sig = (index >= delay) ? z[index - delay] : NAN;
            int reason = -1;
            if (!isnan(exit_sig) && fabs(exit_sig) <= exit_z) {
                reason = 0; /* z_reverted */
            } else {
                double held = ts_days[index] - entry_ts;
                if (held >= max_hold_days) {
                    reason = 1; /* max_hold */
                } else if (dte[index] <= close_before) {
                    reason = 2; /* near_expiry_buffer */
                }
            }
            if (reason >= 0) {
                if (append_trade(trades, Py_BuildValue("(nniind)", entry_idx, index, position, reason, signal_idx, z[signal_idx])) < 0) goto fail;
                position = 0;
            }
        }
        Py_ssize_t sig = index - delay;
        if (position == 0 && sig >= 0 && sig_el[sig] && ent_el[index]) {
            double zs = z[sig];
            if (!isnan(zs) && dte[index] > close_before && fabs(zs) >= entry_z) {
                position = (zs > 0.0) ? -1 : 1;
                entry_idx = index;
                signal_idx = sig;
                entry_ts = ts_days[index];
            }
        }
    }
    if (position != 0 && close_at_end) {
        if (append_trade(trades, Py_BuildValue("(nniind)", entry_idx, n - 1, position, 3, signal_idx, z[signal_idx])) < 0) goto fail;
    }

    buf_release_all(buffers, 6);
    return trades;

fail:
    buf_release_all(buffers, 6);
    Py_DECREF(trades);
    return NULL;
}

// NOLINTNEXTLINE(readability-function-cognitive-complexity): baselined legacy C99 hot loop
static PyObject *fast_calendar_convergence_scan(PyObject *Py_UNUSED(self), PyObject *args) {
    PyObject *dev_obj, *dte_obj, *sig_el_obj, *ent_el_obj, *ext_el_obj;
    double threshold;
    Py_ssize_t delay;
    long exit_days;
    int close_at_end;

    if (!PyArg_ParseTuple(
            args, "OOOOOdnlp",
            &dev_obj, &dte_obj, &sig_el_obj, &ent_el_obj, &ext_el_obj,
            &threshold, &delay, &exit_days, &close_at_end)) {
        return NULL;
    }
    Py_ssize_t n = PyObject_Size(dev_obj);
    if (n < 0) return NULL;
    PyObject *trades = PyList_New(0);
    if (!trades || n == 0) return trades;

    BufArg buffers[5] = {0};
    const double *dev = NULL;
    const int32_t *dte = NULL;
    const unsigned char *sig_el = NULL, *ent_el = NULL, *ext_el = NULL;

    if (buf_get(dev_obj, n, &buffers[0], BUF_DOUBLE) < 0 ||
        buf_get(dte_obj, n, &buffers[1], BUF_INT32) < 0 ||
        buf_get(sig_el_obj, n, &buffers[2], BUF_BYTE) < 0 ||
        buf_get(ent_el_obj, n, &buffers[3], BUF_BYTE) < 0 ||
        buf_get(ext_el_obj, n, &buffers[4], BUF_BYTE) < 0) {
        goto fail;
    }
    dev = buf_data(&buffers[0]);
    dte = buf_data(&buffers[1]);
    sig_el = buf_data(&buffers[2]);
    ent_el = buf_data(&buffers[3]);
    ext_el = buf_data(&buffers[4]);

    int position = 0;
    Py_ssize_t entry_idx = 0, signal_idx = 0;

    for (Py_ssize_t index = 0; index < n; index++) {
        if (position != 0 && ext_el[index] && dte[index] <= exit_days) {
            if (append_trade(trades, Py_BuildValue("(nniind)", entry_idx, index, position, 2, signal_idx, dev[signal_idx])) < 0) goto fail;
            position = 0;
        }
        Py_ssize_t sig = index - delay;
        if (position == 0 && sig >= 0 && sig_el[sig] && ent_el[index] && dte[index] > exit_days) {
            double dv = dev[sig];
            if (!isnan(dv) && fabs(dv) >= threshold) {
                position = (dv > 0.0) ? -1 : 1;
                entry_idx = index;
                signal_idx = sig;
            }
        }
    }
    if (position != 0 && close_at_end) {
        if (append_trade(trades, Py_BuildValue("(nniind)", entry_idx, n - 1, position, 3, signal_idx, dev[signal_idx])) < 0) goto fail;
    }

    buf_release_all(buffers, 5);
    return trades;

fail:
    buf_release_all(buffers, 5);
    Py_DECREF(trades);
    return NULL;
}

/* ---------------- 4. Recursive clean_obj (proof.py & calendar_spreads.py) ---------------- */

static PyObject *clean_recursive(PyObject *value);

static PyObject *clean_dict_items(PyObject *value, int exact) {
    PyObject *out = exact ? _PyDict_NewPresized(PyDict_GET_SIZE(value)) : PyDict_New();
    if (!out) return NULL;
    PyObject *key, *item;
    Py_ssize_t pos = 0;
    while (PyDict_Next(value, &pos, &key, &item)) {
        PyObject *cleaned = clean_recursive(item);
        if (!cleaned) {
            Py_DECREF(out);
            return NULL;
        }
        int rc = PyDict_SetItem(out, key, cleaned);
        Py_DECREF(cleaned);
        if (rc < 0) {
            Py_DECREF(out);
            return NULL;
        }
    }
    return out;
}

static PyObject *clean_sequence_items(PyObject *value, int is_tuple) {
    Py_ssize_t n = is_tuple ? PyTuple_GET_SIZE(value) : PyList_GET_SIZE(value);
    PyObject *out = is_tuple ? PyTuple_New(n) : PyList_New(n);
    if (!out) return NULL;
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *item = is_tuple ? PyTuple_GET_ITEM(value, i) : PyList_GET_ITEM(value, i);
        PyObject *cleaned = clean_recursive(item);
        if (!cleaned) {
            Py_DECREF(out);
            return NULL;
        }
        if (is_tuple) PyTuple_SET_ITEM(out, i, cleaned);
        else PyList_SET_ITEM(out, i, cleaned);
    }
    return out;
}

static PyObject *clean_recursive(PyObject *value) {
    if (value == Py_None || PyUnicode_CheckExact(value) || PyLong_CheckExact(value) || PyBool_Check(value)) {
        Py_INCREF(value);
        return value;
    }
    if (PyFloat_Check(value)) {
        double v = PyFloat_CheckExact(value) ? PyFloat_AS_DOUBLE(value) : PyFloat_AsDouble(value);
        if (v == -1.0 && PyErr_Occurred()) return NULL;
        if (!isfinite(v)) Py_RETURN_NONE;
        Py_INCREF(value);
        return value;
    }
    if (PyDict_Check(value)) return clean_dict_items(value, PyDict_CheckExact(value));
    if (PyList_Check(value)) return clean_sequence_items(value, 0);
    if (PyTuple_Check(value)) return clean_sequence_items(value, 1);
    Py_INCREF(value);
    return value;
}

static PyObject *fast_clean_obj(PyObject *Py_UNUSED(self), PyObject *arg) {
    return clean_recursive(arg);
}

/* ---------------- 5. Pair mean_reversion scan  ---------------- */

// NOLINTNEXTLINE(readability-function-cognitive-complexity): baselined legacy C99 hot loop
static PyObject *fast_mean_reversion_scan(PyObject *Py_UNUSED(self), PyObject *args) {
    PyObject *z_list;
    Py_ssize_t row_count, lookback, delay;
    double entry_z, exit_z;
    int want_positions;
    if (!PyArg_ParseTuple(args, "O!nnnddp",
                          &PyList_Type, &z_list,
                          &row_count, &lookback, &delay,
                          &entry_z, &exit_z, &want_positions)) {
        return NULL;
    }
    if (PyList_GET_SIZE(z_list) != row_count) {
        PyErr_SetString(PyExc_ValueError, "z_list length mismatch");
        return NULL;
    }
    PyObject *trades = PyList_New(0);
    if (!trades) return NULL;
    PyObject *actions = want_positions ? PyList_New(row_count) : NULL;
    if (want_positions && !actions) {
        Py_DECREF(trades);
        return NULL;
    }

    int position = 0;
    Py_ssize_t entry_idx = -1;
    double entry_signal_z = 0.0;
    Py_ssize_t start = lookback + delay;

    for (Py_ssize_t index = 0; index < row_count; index++) {
        int action = (position == 0) ? 0 : 1;
        if (index >= start) {
            PyObject *z_obj = PyList_GET_ITEM(z_list, index);
            if (z_obj != Py_None) {
                double signal_z = PyFloat_CheckExact(z_obj) ? PyFloat_AS_DOUBLE(z_obj) : PyFloat_AsDouble(z_obj);
                if (signal_z == -1.0 && PyErr_Occurred()) {
                    goto fail;
                }
                double az = fabs(signal_z);
                if (position != 0 && az <= exit_z) {
                    if (append_trade(trades, Py_BuildValue("(nnidi)", entry_idx, index, position, entry_signal_z, 0)) < 0) {
                        goto fail;
                    }
                    position = 0;
                    entry_idx = -1;
                    action = 2;
                } else if (position == 0 && index < row_count - 1 && az >= entry_z) {
                    position = (signal_z < 0.0) ? 1 : -1;
                    entry_idx = index;
                    entry_signal_z = signal_z;
                    action = (position > 0) ? 3 : 4;
                }
            }
        }
        if (want_positions) {
            PyObject *pair = Py_BuildValue("(ii)", action, position);
            if (!pair) {
                goto fail;
            }
            PyList_SET_ITEM(actions, index, pair);
        }
    }

    if (position != 0 && row_count > 0) {
        if (append_trade(trades, Py_BuildValue("(nnidi)", entry_idx, row_count - 1, position, entry_signal_z, 1)) < 0) {
            goto fail;
        }
        if (want_positions) {
            PyObject *pair = Py_BuildValue("(ii)", 5, position);
            if (!pair) {
                goto fail;
            }
            PyObject *old = PyList_GET_ITEM(actions, row_count - 1);
            PyList_SET_ITEM(actions, row_count - 1, pair);
            Py_DECREF(old);
        }
    }

    PyObject *ret = PyTuple_Pack(2, trades, want_positions ? actions : Py_None);
    Py_DECREF(trades);
    Py_XDECREF(actions);
    return ret;

fail:
    Py_DECREF(trades);
    Py_XDECREF(actions);
    return NULL;
}

/* ---------------- 6. Max-affordable sizing  ----------------
 * Accepts zero-copy array('d') / array('B') buffers or Python lists for all
 * window columns, and supports `scalar_only=1` (when rows_lst == Py_None and
 * summary_only flag is set, or via optional `scalar_only` arg) to skip allocating
 * `n` Python ledger dicts + `2*n` PyFloats per Optuna training trial!
 */

typedef struct {
    Py_ssize_t orig_idx;
    Py_ssize_t entry_idx;
    Py_ssize_t exit_idx;
    int sign;
    int open_at_end;
    double entry_spread_fill;
    double exit_spread_fill;
    double entry_margin;
    double ec[3];
    double xc[3];
    double exit_point;
    Py_ssize_t quantity;
    double margin_rub;
    double gross_rub;
    double entry_scaled[3];
    double exit_scaled[3];
    int has_exit_costs;
} TradeSpec;

static int quantity_requirement(Py_ssize_t q, const double uc[3], double margin,
                                double reserve, double *out) {
    double f0, f1, f2;
    if (exact_round2(uc[0] * (double)q, &f0) < 0 ||
        exact_round2(uc[1] * (double)q, &f1) < 0 ||
        exact_round2(uc[2] * (double)q, &f2) < 0) return -1;
    *out = margin * (double)q + reserve + 2.0 * (f0 + f1 + f2);
    return 0;
}

// NOLINTNEXTLINE(readability-function-cognitive-complexity): baselined legacy C99 helper
static int calc_max_quantity(
    double budget, double margin, const double uc[3], int stress, int native_spread, Py_ssize_t *out_q
) {
    if (!isfinite(budget) || budget <= 0.0) {
        *out_q = 0;
        return 0;
    }
    double fixed_reserve = (stress && !native_spread) ? UNCERTAINTY_BUFFER : 0.0;
    double per_pair_reserve = stress ? (STRESS_VM_BUFFER + (native_spread ? UNCERTAINTY_BUFFER : 0.0)) : 0.0;
    double required_margin = (stress ? 3.0 : 1.0) * margin + per_pair_reserve;
    double uc_sum = uc[0] + uc[1] + uc[2];
    double raw = floor((budget - fixed_reserve) / (required_margin + 2.0 * uc_sum));
    Py_ssize_t q = (raw > 0.0) ? (Py_ssize_t)raw : 0;

    while (q > 0) {
        double req;
        if (quantity_requirement(q, uc, required_margin, fixed_reserve, &req) < 0) return -1;
        if (req <= budget + 1e-9) break;
        q--;
    }
    while (1) {
        Py_ssize_t q1 = q + 1;
        double req;
        if (quantity_requirement(q1, uc, required_margin, fixed_reserve, &req) < 0) return -1;
        if (req > budget + 1e-9) break;
        q = q1;
    }
    *out_q = q;
    return 0;
}

static int parse_double_item(PyObject *obj, double *out) {
    double v = PyFloat_CheckExact(obj) ? PyFloat_AS_DOUBLE(obj) : PyFloat_AsDouble(obj);
    if (v == -1.0 && PyErr_Occurred()) return -1;
    *out = v;
    return 0;
}

typedef struct { int stress, native_spread, funding_only, paused; double initial; } SizingRules;

typedef struct {
    Py_ssize_t i;
    int is_elig, is_exit_elig, unit_funding_contracts;
    double spread_i, point_i, pos_mark, daily, cost, fill_wealth;
    TradeSpec *pos_trade, *funding_owner;
} SizingBarStep;

static int parse_one_trade_spec(PyObject *tup, TradeSpec *sp) {
    if (!PyTuple_Check(tup) || PyTuple_GET_SIZE(tup) != 15) {
        PyErr_SetString(PyExc_ValueError, "invalid trade spec tuple");
        return -1;
    }
    memset(sp, 0, sizeof(TradeSpec));
    sp->orig_idx = PyLong_AsSsize_t(PyTuple_GET_ITEM(tup, 0));
    sp->entry_idx = PyLong_AsSsize_t(PyTuple_GET_ITEM(tup, 1));
    sp->exit_idx = PyLong_AsSsize_t(PyTuple_GET_ITEM(tup, 2));
    sp->sign = (int)PyLong_AsLong(PyTuple_GET_ITEM(tup, 3));
    sp->open_at_end = PyObject_IsTrue(PyTuple_GET_ITEM(tup, 4));
    if (PyErr_Occurred()) return -1;
    if (parse_double_item(PyTuple_GET_ITEM(tup, 5), &sp->entry_spread_fill) < 0 ||
        parse_double_item(PyTuple_GET_ITEM(tup, 6), &sp->exit_spread_fill) < 0 ||
        parse_double_item(PyTuple_GET_ITEM(tup, 7), &sp->entry_margin) < 0 ||
        parse_double_item(PyTuple_GET_ITEM(tup, 8), &sp->ec[0]) < 0 ||
        parse_double_item(PyTuple_GET_ITEM(tup, 9), &sp->ec[1]) < 0 ||
        parse_double_item(PyTuple_GET_ITEM(tup, 10), &sp->ec[2]) < 0 ||
        parse_double_item(PyTuple_GET_ITEM(tup, 11), &sp->xc[0]) < 0 ||
        parse_double_item(PyTuple_GET_ITEM(tup, 12), &sp->xc[1]) < 0 ||
        parse_double_item(PyTuple_GET_ITEM(tup, 13), &sp->xc[2]) < 0 ||
        parse_double_item(PyTuple_GET_ITEM(tup, 14), &sp->exit_point) < 0) {
        return -1;
    }
    return 0;
}

static int parse_trade_specs(PyObject *specs_lst, Py_ssize_t n_trades, TradeSpec **out_specs) {
    *out_specs = NULL;
    if (n_trades <= 0) return 0;
    TradeSpec *specs = (TradeSpec *)FASTCORE_MALLOC((size_t)n_trades * sizeof(TradeSpec));
    if (!specs) {
        PyErr_NoMemory();
        return -1;
    }
    for (Py_ssize_t t = 0; t < n_trades; t++) {
        if (parse_one_trade_spec(PyList_GET_ITEM(specs_lst, t), &specs[t]) < 0) {
            FASTCORE_FREE(specs);
            return -1;
        }
    }
    *out_specs = specs;
    return 0;
}

static int extract_row_funding(PyObject *row, double *out_unit_funding, int *out_contracts) {
    *out_unit_funding = 0.0;
    *out_contracts = 0;
    PyObject *uf_obj = PyDict_GetItemWithError(row, k_funding_rub);
    if (!uf_obj && PyErr_Occurred()) return -1;
    if (uf_obj && uf_obj != Py_None && parse_double_item(uf_obj, out_unit_funding) < 0) {
        return -1;
    }
    PyObject *uc_obj = PyDict_GetItemWithError(row, k_funding_contracts);
    if (!uc_obj && PyErr_Occurred()) return -1;
    if (uc_obj && uc_obj != Py_None) {
        *out_contracts = (int)PyLong_AsLong(uc_obj);
        if (*out_contracts == -1 && PyErr_Occurred()) return -1;
    }
    return 0;
}

static int scale_cost_triplet(const double src[3], double qd, double dst[3], double *out_fee) {
    if (exact_round2(src[0] * qd, &dst[0]) < 0 ||
        exact_round2(src[1] * qd, &dst[1]) < 0 ||
        exact_round2(src[2] * qd, &dst[2]) < 0) {
        return -1;
    }
    *out_fee = dst[0] + dst[1] + dst[2];
    return 0;
}

static int apply_trade_exit_costs(TradeSpec *tr, double qd, int funding_only, double *out_fee) {
    *out_fee = 0.0;
    if (tr->open_at_end && funding_only) {
        tr->has_exit_costs = 0;
        return 0;
    }
    tr->has_exit_costs = 1;
    return scale_cost_triplet(tr->xc, qd, tr->exit_scaled, out_fee);
}

static int append_ssize_item(PyObject *lst, Py_ssize_t val) {
    PyObject *obj = PyLong_FromSsize_t(val);
    if (!obj) return -1;
    int rc = PyList_Append(lst, obj);
    Py_DECREF(obj);
    return rc;
}

static int step_open_position(SizingBarStep *st, int funding_only) {
    TradeSpec *tr = st->pos_trade;
    if (tr == NULL) return 0;
    int exiting = (st->i == tr->exit_idx);
    int should_exit = exiting && (st->is_exit_elig || tr->open_at_end);
    int should_mark = should_exit || (st->is_elig && !exiting);
    if (!should_mark) {
        st->funding_owner = tr;
        return 0;
    }
    double end_val = should_exit ? tr->exit_spread_fill : st->spread_i;
    double pt = should_exit ? tr->exit_point : st->point_i;
    double qd = (double)tr->quantity;
    double chg = 0.0;
    if (exact_round2(tr->sign * (end_val - st->pos_mark) * pt * qd, &chg) < 0) return -1;
    tr->gross_rub += chg;
    st->pos_mark = end_val;
    st->daily += chg;
    if (!should_exit) {
        st->funding_owner = tr;
        return 0;
    }
    if (st->unit_funding_contracts) st->funding_owner = tr;
    double fee = 0.0;
    if (apply_trade_exit_costs(tr, qd, funding_only, &fee) < 0) return -1;
    if (tr->has_exit_costs) {
        st->cost += fee;
        st->fill_wealth += chg - fee;
    }
    st->pos_trade = NULL;
    return 0;
}

static int try_open_candidate(
    SizingBarStep *st, TradeSpec *cand, Py_ssize_t cand_idx,
    const SizingRules *rules, PyObject *accepted_out
) {
    if (st->pos_trade != NULL) return 1; /* overlap */
    double budget = fmax(0.0, fmin(st->fill_wealth, rules->initial));
    Py_ssize_t q = 0;
    if (!rules->paused &&
        calc_max_quantity(budget, cand->entry_margin, cand->ec, rules->stress, rules->native_spread, &q) < 0) {
        return -1;
    }
    if (q <= 0) return 0;

    double qd = (double)q;
    cand->quantity = q;
    cand->gross_rub = 0.0;
    double entry_fee = 0.0;
    if (exact_round2(cand->entry_margin * qd, &cand->margin_rub) < 0 ||
        scale_cost_triplet(cand->ec, qd, cand->entry_scaled, &entry_fee) < 0 ||
        append_ssize_item(accepted_out, cand_idx) < 0) {
        return -1;
    }
    st->cost += entry_fee;
    st->pos_trade = cand;
    st->pos_mark = cand->entry_spread_fill;
    st->funding_owner = cand;
    int same_day_exit = (cand->exit_idx == st->i);
    if (st->is_elig) {
        double end_val = same_day_exit ? cand->exit_spread_fill : st->spread_i;
        double chg = 0.0;
        if (exact_round2(cand->sign * (end_val - st->pos_mark) * st->point_i * qd, &chg) < 0) return -1;
        cand->gross_rub += chg;
        st->pos_mark = end_val;
        st->daily += chg;
    }
    if (!same_day_exit) return 0;
    double exit_fee = 0.0;
    if (apply_trade_exit_costs(cand, qd, rules->funding_only, &exit_fee) < 0) return -1;
    st->cost += exit_fee;
    st->pos_trade = NULL;
    return 0;
}

static int emit_ledger_row(
    PyObject *ledger, Py_ssize_t i, PyObject *date_obj,
    double cumulative, double wealth, int shortfall
) {
    PyObject *entry = _PyDict_NewPresized(4);
    PyObject *cum_obj = PyFloat_FromDouble(cumulative);
    PyObject *w_obj = PyFloat_FromDouble(wealth);
    PyObject *sf_obj = shortfall ? Py_True : Py_False;
    if (!entry || !cum_obj || !w_obj) {
        Py_XDECREF(entry); Py_XDECREF(cum_obj); Py_XDECREF(w_obj);
        return -1;
    }
    Py_INCREF(sf_obj);
    int rc = (PyDict_SetItem(entry, k_date, date_obj) < 0 ||
              PyDict_SetItem(entry, k_cumulative_net_rub, cum_obj) < 0 ||
              PyDict_SetItem(entry, k_account_value_rub, w_obj) < 0 ||
              PyDict_SetItem(entry, k_cash_shortfall, sf_obj) < 0)
                 ? -1
                 : 0;
    Py_DECREF(cum_obj); Py_DECREF(w_obj); Py_DECREF(sf_obj);
    if (rc < 0) {
        Py_DECREF(entry);
        return -1;
    }
    PyList_SET_ITEM(ledger, i, entry);
    return 0;
}

static PyObject *build_accepted_tuples(
    PyObject *accepted_out, const TradeSpec *specs, int scalar_only
) {
    Py_ssize_t n_acc = PyList_GET_SIZE(accepted_out);
    PyObject *acc_tuples = PyList_New(n_acc);
    if (!acc_tuples) return NULL;
    if (n_acc > 0 && specs == NULL) {
        Py_DECREF(acc_tuples);
        PyErr_SetString(PyExc_RuntimeError, "accepted trades present without trade specs");
        return NULL;
    }
    for (Py_ssize_t a = 0; a < n_acc; a++) {
        Py_ssize_t sp_idx = PyLong_AsSsize_t(PyList_GET_ITEM(accepted_out, a));
        const TradeSpec *sp = &specs[sp_idx];
        PyObject *tup = NULL;
        if (scalar_only) {
            tup = Py_BuildValue("(nn)", sp->entry_idx, sp->exit_idx);
        } else {
            double gross_rounded = 0.0;
            if (exact_round2(sp->gross_rub, &gross_rounded) < 0) {
                Py_DECREF(acc_tuples);
                return NULL;
            }
            tup = Py_BuildValue(
                "(nnddddddddi)",
                sp->orig_idx, sp->quantity, sp->margin_rub, gross_rounded,
                sp->entry_scaled[0], sp->entry_scaled[1], sp->entry_scaled[2],
                sp->exit_scaled[0], sp->exit_scaled[1], sp->exit_scaled[2],
                sp->has_exit_costs
            );
        }
        if (!tup) {
            Py_DECREF(acc_tuples);
            return NULL;
        }
        PyList_SET_ITEM(acc_tuples, a, tup);
    }
    return acc_tuples;
}

// NOLINTNEXTLINE(readability-function-cognitive-complexity,readability-function-size): CPython buffer & state orchestration
static PyObject *fast_size_proof_metrics(PyObject *Py_UNUSED(self), PyObject *args) {
    PyObject *dates_lst;
    PyObject *prices_obj, *spreads_obj, *points_obj, *posted_obj;
    PyObject *elig_obj, *entry_elig_obj, *exit_elig_obj, *rows_lst, *specs_lst;
    double initial;
    int stress, native_spread, funding_only, has_funding;
    int scalar_only = 0;
    Py_ssize_t start_idx = 0, end_idx = -1;

    if (!PyArg_ParseTuple(
            args, "O!OOOOOOOO!O!dpppp|pnn", &PyList_Type, &dates_lst,
            &prices_obj, &spreads_obj, &points_obj, &posted_obj, &elig_obj, &entry_elig_obj, &exit_elig_obj,
            &PyList_Type, &rows_lst, &PyList_Type, &specs_lst,
            &initial, &stress, &native_spread, &funding_only, &has_funding,
            &scalar_only, &start_idx, &end_idx)) {
        return NULL;
    }
    if (ensure_interned_keys() < 0) return NULL;

    Py_ssize_t n = PyList_GET_SIZE(dates_lst);
    if (end_idx < 0 || end_idx >= n) end_idx = n - 1;
    if (start_idx < 0) start_idx = 0;

    Py_ssize_t n_trades = PyList_GET_SIZE(specs_lst);
    BufArg buffers[7] = {0};
    const double *prices = NULL, *spreads = NULL, *points = NULL, *posted_units = NULL;
    const unsigned char *elig = NULL, *entry_elig = NULL, *exit_elig = NULL;

    TradeSpec *specs = NULL;
    PyObject *ledger = NULL;
    PyObject *accepted_out = NULL;
    PyObject *ret_obj = NULL;

    if (buf_get(prices_obj, n, &buffers[0], BUF_DOUBLE) < 0 ||
        buf_get(spreads_obj, n, &buffers[1], BUF_DOUBLE) < 0 ||
        buf_get(points_obj, n, &buffers[2], BUF_DOUBLE) < 0 ||
        buf_get(posted_obj, n, &buffers[3], BUF_DOUBLE) < 0 ||
        buf_get(elig_obj, n, &buffers[4], BUF_BYTE) < 0 ||
        buf_get(entry_elig_obj, n, &buffers[5], BUF_BYTE) < 0 ||
        buf_get(exit_elig_obj, n, &buffers[6], BUF_BYTE) < 0 ||
        parse_trade_specs(specs_lst, n_trades, &specs) < 0) {
        goto cleanup;
    }
    prices = buf_data(&buffers[0]);
    spreads = buf_data(&buffers[1]);
    points = buf_data(&buffers[2]);
    posted_units = buf_data(&buffers[3]);
    elig = buf_data(&buffers[4]);
    entry_elig = buf_data(&buffers[5]);
    exit_elig = buf_data(&buffers[6]);


    if (!scalar_only) {
        ledger = PyList_New(n);
        if (!ledger) goto cleanup;
    }
    accepted_out = PyList_New(0);
    if (!accepted_out) goto cleanup;

    double cumulative = 0.0, free_cash = initial, posted = 0.0;
    double wealth = initial;
    SizingRules rules = {stress, native_spread, funding_only, 0, initial};
    double p0 = (n > 0) ? prices[0] : 1.0;

    double end_wealth = initial, end_cum_net = 0.0, end_price = p0;
    int any_window_shortfall = 0;

    TradeSpec *pos_trade = NULL;
    double pos_mark = 0.0;
    Py_ssize_t next_trade_cursor = 0;

    for (Py_ssize_t i = 0; i < n; i++) {
        double unit_funding = 0.0;
        int unit_funding_contracts = 0;
        if (has_funding &&
            extract_row_funding(PyList_GET_ITEM(rows_lst, i), &unit_funding, &unit_funding_contracts) < 0) {
            goto cleanup;
        }

        int is_elig = elig[i];
        int is_entry_elig = entry_elig[i];
        int is_exit_elig = exit_elig[i];
        SizingBarStep step = {
            .i = i, .is_elig = is_elig, .is_exit_elig = is_exit_elig, .unit_funding_contracts = unit_funding_contracts,
            .spread_i = spreads[i], .point_i = points[i], .pos_mark = pos_mark, .fill_wealth = wealth, .pos_trade = pos_trade
        };

        if (step_open_position(&step, funding_only) < 0) {
            goto cleanup;
        }

        while (next_trade_cursor < n_trades && specs[next_trade_cursor].entry_idx < i) {
            next_trade_cursor++;
        }
        TradeSpec *candidate = (next_trade_cursor < n_trades && specs[next_trade_cursor].entry_idx == i)
                                   ? &specs[next_trade_cursor]
                                   : NULL;
        if (candidate != NULL && is_entry_elig) {
            int open_rc = try_open_candidate(&step, candidate, next_trade_cursor, &rules, accepted_out);
            if (open_rc < 0) goto cleanup;
            if (open_rc > 0) {
                ret_obj = Py_BuildValue("(sO)", "overlap", Py_None);
                goto cleanup;
            }
        }

        pos_trade = step.pos_trade;
        pos_mark = step.pos_mark;
        Py_ssize_t funding_q = (step.funding_owner != NULL && unit_funding_contracts)
                                   ? step.funding_owner->quantity
                                   : 0;
        double funding = 0.0;
        if (exact_round2(unit_funding * (double)funding_q, &funding) < 0) goto cleanup;
        if (step.funding_owner != NULL) {
            step.funding_owner->gross_rub += funding;
        }
        if (exact_round2(step.daily + funding, &step.daily) < 0 ||
            exact_round2(step.cost, &step.cost) < 0 ||
            exact_round2(cumulative + step.daily - step.cost, &cumulative) < 0) {
            goto cleanup;
        }
        double growth = (i > 0) ? (prices[i] / prices[i - 1]) : 1.0;
        wealth = fmax(free_cash, 0.0) * growth + fmin(free_cash, 0.0) + posted + step.daily - step.cost;
        if (pos_trade == NULL) posted = 0.0;
        else if (is_elig || is_entry_elig) posted = posted_units[i] * (double)pos_trade->quantity;
        free_cash = wealth - posted;
        int shortfall = (free_cash < -1e-7);
        rules.paused |= shortfall;

        if (scalar_only) {
            if (i >= start_idx && i <= end_idx && shortfall) {
                any_window_shortfall = 1;
            }
            if (i == end_idx) {
                end_wealth = wealth;
                end_cum_net = cumulative;
                end_price = prices[i];
            }
        } else if (emit_ledger_row(ledger, i, PyList_GET_ITEM(dates_lst, i), cumulative, wealth, shortfall) < 0) {
            goto cleanup;
        }
    }

    PyObject *acc_tuples = build_accepted_tuples(accepted_out, specs, scalar_only);
    if (!acc_tuples) goto cleanup;

    if (scalar_only) {
        PyObject *summary = Py_BuildValue("(ddddi)", p0, end_price, end_wealth, end_cum_net, any_window_shortfall);
        if (summary) {
            ret_obj = PyTuple_Pack(2, summary, acc_tuples);
            Py_DECREF(summary);
        }
    } else {
        ret_obj = PyTuple_Pack(2, ledger, acc_tuples);
    }
    Py_DECREF(acc_tuples);

cleanup:
    Py_XDECREF(ledger);
    Py_XDECREF(accepted_out);
    buf_release_all(buffers, 7);
    FASTCORE_FREE(specs);
    return ret_obj;
}

static PyMethodDef FastCoreMethods[] = {
    {"compute_rolling_z", fast_compute_rolling_z, METH_VARARGS, "Exact population rolling z-score."},
    {"calendar_sample_z", fast_calendar_sample_z, METH_VARARGS, "Exact sample rolling z-score matching statistics.mean/stdev."},
    {"calendar_zscore_scan", fast_calendar_zscore_scan, METH_VARARGS, "Exact calendar zscore_trades scan."},
    {"calendar_convergence_scan", fast_calendar_convergence_scan, METH_VARARGS, "Exact calendar convergence_trades scan."},
    {"clean_obj", fast_clean_obj, METH_O, "Recursively replace non-finite floats with None."},
    {"mean_reversion_scan", fast_mean_reversion_scan, METH_VARARGS, "Fast mean reversion state machine scan."},
    {"size_proof_metrics", fast_size_proof_metrics, METH_VARARGS, "Exact metrics_only max-affordable sizing scan."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef fastcore_module = {
    PyModuleDef_HEAD_INIT,
    "_fastcore",
    "Optional C99 accelerator for exact rolling z-score, calendar scans, payload cleaning, MR scan, and sizing.",
    -1,
    FastCoreMethods,
    NULL,
    NULL,
    NULL,
    NULL
};

PyMODINIT_FUNC PyInit__fastcore(void) {
    return PyModule_Create(&fastcore_module);
}
