#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

#define STRESS_VM_BUFFER 5000.0
#define UNCERTAINTY_BUFFER 5000.0

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

static PyObject *fast_compute_rolling_z(PyObject *self, PyObject *args) {
    PyObject *spread_list;
    Py_ssize_t lookback, delay;
    if (!PyArg_ParseTuple(args, "O!nn", &PyList_Type, &spread_list, &lookback, &delay)) {
        return NULL;
    }
    if (lookback <= 0 || delay < 0) {
        PyErr_SetString(PyExc_ValueError, "invalid lookback or delay");
        return NULL;
    }
    Py_ssize_t n = PyList_GET_SIZE(spread_list);
    PyObject *result = PyList_New(n);
    if (!result) return NULL;
    if (n == 0) return result;

    double *x = (double *)PyMem_Malloc((size_t)n * sizeof(double));
    if (!x) {
        Py_DECREF(result);
        return PyErr_NoMemory();
    }
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *item = PyList_GET_ITEM(spread_list, i);
        double v = PyFloat_CheckExact(item) ? PyFloat_AS_DOUBLE(item) : PyFloat_AsDouble(item);
        if (v == -1.0 && PyErr_Occurred()) {
            PyMem_Free(x);
            Py_DECREF(result);
            return NULL;
        }
        x[i] = v;
    }

    Py_ssize_t start = lookback + delay;
    Py_ssize_t pre = start < n ? start : n;
    for (Py_ssize_t i = 0; i < pre; i++) {
        Py_INCREF(Py_None);
        PyList_SET_ITEM(result, i, Py_None);
    }
    double lb = (double)lookback;
    for (Py_ssize_t index = start; index < n; index++) {
        Py_ssize_t signal_index = index - delay;
        Py_ssize_t w0 = signal_index - lookback;
        double m_hi = 0.0, m_lo = 0.0;
        for (Py_ssize_t j = w0; j < signal_index; j++) {
            double v = x[j];
            double t = m_hi + v;
            if (fabs(m_hi) >= fabs(v)) {
                m_lo += (m_hi - t) + v;
            } else {
                m_lo += (v - t) + m_hi;
            }
            m_hi = t;
        }
        double mean = (m_hi + m_lo) / lb;
        double v_hi = 0.0, v_lo = 0.0;
        for (Py_ssize_t j = w0; j < signal_index; j++) {
            double d = x[j] - mean;
            double sq = d * d;
            double t = v_hi + sq;
            if (fabs(v_hi) >= fabs(sq)) {
                v_lo += (v_hi - t) + sq;
            } else {
                v_lo += (sq - t) + v_hi;
            }
            v_hi = t;
        }
        double std = sqrt((v_hi + v_lo) / lb);
        double z = (std > 0.0) ? ((x[signal_index] - mean) / std) : 0.0;
        PyObject *zf = PyFloat_FromDouble(z);
        if (!zf) {
            PyMem_Free(x);
            Py_DECREF(result);
            return NULL;
        }
        PyList_SET_ITEM(result, index, zf);
    }
    PyMem_Free(x);
    return result;
}

static PyObject *clean_recursive(PyObject *value) {
    if (value == Py_None || PyUnicode_CheckExact(value) || PyLong_CheckExact(value) || PyBool_Check(value)) {
        Py_INCREF(value);
        return value;
    }
    if (PyFloat_CheckExact(value)) {
        if (!isfinite(PyFloat_AS_DOUBLE(value))) {
            Py_RETURN_NONE;
        }
        Py_INCREF(value);
        return value;
    }
    if (PyFloat_Check(value)) {
        double v = PyFloat_AsDouble(value);
        if (v == -1.0 && PyErr_Occurred()) return NULL;
        if (!isfinite(v)) {
            Py_RETURN_NONE;
        }
        Py_INCREF(value);
        return value;
    }
    if (PyDict_CheckExact(value)) {
        PyObject *out = _PyDict_NewPresized(PyDict_GET_SIZE(value));
        if (!out) return NULL;
        PyObject *key, *item;
        Py_ssize_t pos = 0;
        while (PyDict_Next(value, &pos, &key, &item)) {
            PyObject *cleaned = clean_recursive(item);
            if (!cleaned) {
                Py_DECREF(out);
                return NULL;
            }
            if (PyDict_SetItem(out, key, cleaned) < 0) {
                Py_DECREF(cleaned);
                Py_DECREF(out);
                return NULL;
            }
            Py_DECREF(cleaned);
        }
        return out;
    }
    if (PyList_CheckExact(value)) {
        Py_ssize_t n = PyList_GET_SIZE(value);
        PyObject *out = PyList_New(n);
        if (!out) return NULL;
        for (Py_ssize_t i = 0; i < n; i++) {
            PyObject *cleaned = clean_recursive(PyList_GET_ITEM(value, i));
            if (!cleaned) {
                Py_DECREF(out);
                return NULL;
            }
            PyList_SET_ITEM(out, i, cleaned);
        }
        return out;
    }
    if (PyTuple_CheckExact(value)) {
        Py_ssize_t n = PyTuple_GET_SIZE(value);
        PyObject *out = PyTuple_New(n);
        if (!out) return NULL;
        for (Py_ssize_t i = 0; i < n; i++) {
            PyObject *cleaned = clean_recursive(PyTuple_GET_ITEM(value, i));
            if (!cleaned) {
                Py_DECREF(out);
                return NULL;
            }
            PyTuple_SET_ITEM(out, i, cleaned);
        }
        return out;
    }
    if (PyDict_Check(value)) {
        PyObject *out = PyDict_New();
        if (!out) return NULL;
        PyObject *key, *item;
        Py_ssize_t pos = 0;
        while (PyDict_Next(value, &pos, &key, &item)) {
            PyObject *cleaned = clean_recursive(item);
            if (!cleaned) {
                Py_DECREF(out);
                return NULL;
            }
            if (PyDict_SetItem(out, key, cleaned) < 0) {
                Py_DECREF(cleaned);
                Py_DECREF(out);
                return NULL;
            }
            Py_DECREF(cleaned);
        }
        return out;
    }
    if (PyList_Check(value)) {
        Py_ssize_t n = PyList_Size(value);
        if (n < 0) return NULL;
        PyObject *out = PyList_New(n);
        if (!out) return NULL;
        for (Py_ssize_t i = 0; i < n; i++) {
            PyObject *item = PyList_GetItem(value, i);
            PyObject *cleaned = clean_recursive(item);
            if (!cleaned) {
                Py_DECREF(out);
                return NULL;
            }
            PyList_SET_ITEM(out, i, cleaned);
        }
        return out;
    }
    if (PyTuple_Check(value)) {
        Py_ssize_t n = PyTuple_Size(value);
        if (n < 0) return NULL;
        PyObject *out = PyTuple_New(n);
        if (!out) return NULL;
        for (Py_ssize_t i = 0; i < n; i++) {
            PyObject *item = PyTuple_GetItem(value, i);
            PyObject *cleaned = clean_recursive(item);
            if (!cleaned) {
                Py_DECREF(out);
                return NULL;
            }
            PyTuple_SET_ITEM(out, i, cleaned);
        }
        return out;
    }
    Py_INCREF(value);
    return value;
}

static PyObject *fast_clean_obj(PyObject *self, PyObject *arg) {
    return clean_recursive(arg);
}

static PyObject *fast_mean_reversion_scan(PyObject *self, PyObject *args) {
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
                    Py_DECREF(trades);
                    Py_XDECREF(actions);
                    return NULL;
                }
                double az = fabs(signal_z);
                if (position != 0 && az <= exit_z) {
                    PyObject *tup = Py_BuildValue("(nnidi)", entry_idx, index, position, entry_signal_z, 0);
                    if (!tup || PyList_Append(trades, tup) < 0) {
                        Py_XDECREF(tup);
                        Py_DECREF(trades);
                        Py_XDECREF(actions);
                        return NULL;
                    }
                    Py_DECREF(tup);
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
                Py_DECREF(trades);
                Py_DECREF(actions);
                return NULL;
            }
            PyList_SET_ITEM(actions, index, pair);
        }
    }

    if (position != 0 && row_count > 0) {
        PyObject *tup = Py_BuildValue("(nnidi)", entry_idx, row_count - 1, position, entry_signal_z, 1);
        if (!tup || PyList_Append(trades, tup) < 0) {
            Py_XDECREF(tup);
            Py_DECREF(trades);
            Py_XDECREF(actions);
            return NULL;
        }
        Py_DECREF(tup);
        if (want_positions) {
            PyObject *pair = Py_BuildValue("(ii)", 5, position);
            if (!pair) {
                Py_DECREF(trades);
                Py_DECREF(actions);
                return NULL;
            }
            PyObject *old = PyList_GET_ITEM(actions, row_count - 1);
            PyList_SET_ITEM(actions, row_count - 1, pair);
            Py_DECREF(old);
        }
    }

    if (want_positions) {
        PyObject *ret = PyTuple_Pack(2, trades, actions);
        Py_DECREF(trades);
        Py_DECREF(actions);
        return ret;
    }
    Py_INCREF(Py_None);
    PyObject *ret = PyTuple_Pack(2, trades, Py_None);
    Py_DECREF(trades);
    Py_DECREF(Py_None);
    return ret;
}

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
    /* Runtime state when accepted */
    Py_ssize_t quantity;
    double margin_rub;
    double gross_rub;
    double entry_scaled[3];
    double exit_scaled[3];
    int has_exit_costs;
} TradeSpec;

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
        double f0, f1, f2;
        if (exact_round2(uc[0] * q, &f0) < 0 || exact_round2(uc[1] * q, &f1) < 0 || exact_round2(uc[2] * q, &f2) < 0) {
            return -1;
        }
        double req = required_margin * q + fixed_reserve + 2.0 * (f0 + f1 + f2);
        if (req <= budget + 1e-9) break;
        q--;
    }
    while (1) {
        Py_ssize_t q1 = q + 1;
        double f0, f1, f2;
        if (exact_round2(uc[0] * q1, &f0) < 0 || exact_round2(uc[1] * q1, &f1) < 0 || exact_round2(uc[2] * q1, &f2) < 0) {
            return -1;
        }
        double req = required_margin * q1 + fixed_reserve + 2.0 * (f0 + f1 + f2);
        if (req > budget + 1e-9) break;
        q = q1;
    }
    *out_q = q;
    return 0;
}

static int extract_double_arr(PyObject *lst, Py_ssize_t n, double **out) {
    double *arr = (double *)PyMem_Malloc((size_t)(n > 0 ? n : 1) * sizeof(double));
    if (!arr) {
        PyErr_NoMemory();
        return -1;
    }
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *item = PyList_GET_ITEM(lst, i);
        double v = PyFloat_CheckExact(item) ? PyFloat_AS_DOUBLE(item) : PyFloat_AsDouble(item);
        if (v == -1.0 && PyErr_Occurred()) {
            PyMem_Free(arr);
            return -1;
        }
        arr[i] = v;
    }
    *out = arr;
    return 0;
}

static int extract_char_arr(PyObject *lst, Py_ssize_t n, unsigned char **out) {
    unsigned char *arr = (unsigned char *)PyMem_Malloc((size_t)(n > 0 ? n : 1));
    if (!arr) {
        PyErr_NoMemory();
        return -1;
    }
    for (Py_ssize_t i = 0; i < n; i++) {
        int truth = PyObject_IsTrue(PyList_GET_ITEM(lst, i));
        if (truth < 0) {
            PyMem_Free(arr);
            return -1;
        }
        arr[i] = (unsigned char)truth;
    }
    *out = arr;
    return 0;
}

static int parse_double_item(PyObject *obj, double *out) {
    double v = PyFloat_CheckExact(obj) ? PyFloat_AS_DOUBLE(obj) : PyFloat_AsDouble(obj);
    if (v == -1.0 && PyErr_Occurred()) return -1;
    *out = v;
    return 0;
}

static PyObject *fast_size_proof_metrics(PyObject *self, PyObject *args) {
    PyObject *dates_lst, *prices_lst, *spreads_lst, *points_lst, *posted_lst;
    PyObject *elig_lst, *entry_elig_lst, *exit_elig_lst, *rows_lst, *specs_lst;
    double initial;
    int stress, native_spread, funding_only, has_funding;

    if (!PyArg_ParseTuple(
            args, "O!O!O!O!O!O!O!O!O!O!dpppp",
            &PyList_Type, &dates_lst,
            &PyList_Type, &prices_lst,
            &PyList_Type, &spreads_lst,
            &PyList_Type, &points_lst,
            &PyList_Type, &posted_lst,
            &PyList_Type, &elig_lst,
            &PyList_Type, &entry_elig_lst,
            &PyList_Type, &exit_elig_lst,
            &PyList_Type, &rows_lst,
            &PyList_Type, &specs_lst,
            &initial, &stress, &native_spread, &funding_only, &has_funding)) {
        return NULL;
    }
    if (ensure_interned_keys() < 0) return NULL;

    Py_ssize_t n = PyList_GET_SIZE(dates_lst);
    Py_ssize_t n_trades = PyList_GET_SIZE(specs_lst);
    double *prices = NULL, *spreads = NULL, *points = NULL, *posted_units = NULL;
    unsigned char *elig = NULL, *entry_elig = NULL, *exit_elig = NULL;
    TradeSpec *specs = NULL;
    PyObject *ledger = NULL;
    PyObject *accepted_out = NULL;

    if (extract_double_arr(prices_lst, n, &prices) < 0 ||
        extract_double_arr(spreads_lst, n, &spreads) < 0 ||
        extract_double_arr(points_lst, n, &points) < 0 ||
        extract_double_arr(posted_lst, n, &posted_units) < 0 ||
        extract_char_arr(elig_lst, n, &elig) < 0 ||
        extract_char_arr(entry_elig_lst, n, &entry_elig) < 0 ||
        extract_char_arr(exit_elig_lst, n, &exit_elig) < 0) {
        goto cleanup;
    }

    if (n_trades > 0) {
        specs = (TradeSpec *)PyMem_Malloc((size_t)n_trades * sizeof(TradeSpec));
        if (!specs) {
            PyErr_NoMemory();
            goto cleanup;
        }
        for (Py_ssize_t t = 0; t < n_trades; t++) {
            PyObject *tup = PyList_GET_ITEM(specs_lst, t);
            if (!PyTuple_Check(tup) || PyTuple_GET_SIZE(tup) != 15) {
                PyErr_SetString(PyExc_ValueError, "invalid trade spec tuple");
                goto cleanup;
            }
            TradeSpec *sp = &specs[t];
            memset(sp, 0, sizeof(TradeSpec));
            sp->orig_idx = PyLong_AsSsize_t(PyTuple_GET_ITEM(tup, 0));
            sp->entry_idx = PyLong_AsSsize_t(PyTuple_GET_ITEM(tup, 1));
            sp->exit_idx = PyLong_AsSsize_t(PyTuple_GET_ITEM(tup, 2));
            sp->sign = (int)PyLong_AsLong(PyTuple_GET_ITEM(tup, 3));
            sp->open_at_end = PyObject_IsTrue(PyTuple_GET_ITEM(tup, 4));
            if (PyErr_Occurred() ||
                parse_double_item(PyTuple_GET_ITEM(tup, 5), &sp->entry_spread_fill) < 0 ||
                parse_double_item(PyTuple_GET_ITEM(tup, 6), &sp->exit_spread_fill) < 0 ||
                parse_double_item(PyTuple_GET_ITEM(tup, 7), &sp->entry_margin) < 0 ||
                parse_double_item(PyTuple_GET_ITEM(tup, 8), &sp->ec[0]) < 0 ||
                parse_double_item(PyTuple_GET_ITEM(tup, 9), &sp->ec[1]) < 0 ||
                parse_double_item(PyTuple_GET_ITEM(tup, 10), &sp->ec[2]) < 0 ||
                parse_double_item(PyTuple_GET_ITEM(tup, 11), &sp->xc[0]) < 0 ||
                parse_double_item(PyTuple_GET_ITEM(tup, 12), &sp->xc[1]) < 0 ||
                parse_double_item(PyTuple_GET_ITEM(tup, 13), &sp->xc[2]) < 0 ||
                parse_double_item(PyTuple_GET_ITEM(tup, 14), &sp->exit_point) < 0) {
                goto cleanup;
            }
        }
    }

    ledger = PyList_New(n);
    accepted_out = PyList_New(0);
    if (!ledger || !accepted_out) goto cleanup;

    double cumulative = 0.0, free_cash = initial, posted = 0.0;
    double wealth = initial, peak = 0.0;
    int paused = 0;
    double p0 = (n > 0) ? prices[0] : 1.0;

    TradeSpec *pos_trade = NULL;
    double pos_mark = 0.0;
    Py_ssize_t next_trade_cursor = 0;

    for (Py_ssize_t i = 0; i < n; i++) {
        double unit_funding = 0.0;
        int unit_funding_contracts = 0;
        if (has_funding) {
            PyObject *row = PyList_GET_ITEM(rows_lst, i);
            PyObject *uf_obj = PyDict_GetItem(row, k_funding_rub);
            if (uf_obj && uf_obj != Py_None) {
                if (parse_double_item(uf_obj, &unit_funding) < 0) goto cleanup;
            }
            PyObject *uc_obj = PyDict_GetItem(row, k_funding_contracts);
            if (uc_obj && uc_obj != Py_None) {
                unit_funding_contracts = (int)PyLong_AsLong(uc_obj);
                if (unit_funding_contracts == -1 && PyErr_Occurred()) goto cleanup;
            }
        }

        int is_elig = elig[i];
        int is_entry_elig = entry_elig[i];
        int is_exit_elig = exit_elig[i];
        double daily = 0.0, cost = 0.0;
        TradeSpec *funding_owner = NULL;
        double fill_wealth = wealth;

        if (pos_trade != NULL) {
            int exiting = (i == pos_trade->exit_idx);
            int should_exit = exiting && (is_exit_elig || pos_trade->open_at_end);
            if (should_exit || (is_elig && !exiting)) {
                double end_val = should_exit ? pos_trade->exit_spread_fill : spreads[i];
                double pt = should_exit ? pos_trade->exit_point : points[i];
                double chg;
                if (exact_round2(pos_trade->sign * (end_val - pos_mark) * pt * (double)pos_trade->quantity, &chg) < 0) {
                    goto cleanup;
                }
                pos_trade->gross_rub += chg;
                pos_mark = end_val;
                daily += chg;
                if (should_exit) {
                    if (unit_funding_contracts) {
                        funding_owner = pos_trade;
                    }
                    if (pos_trade->open_at_end && funding_only) {
                        pos_trade->has_exit_costs = 0;
                    } else {
                        pos_trade->has_exit_costs = 1;
                        double qd = (double)pos_trade->quantity;
                        for (int k = 0; k < 3; k++) {
                            if (exact_round2(pos_trade->xc[k] * qd, &pos_trade->exit_scaled[k]) < 0) {
                                goto cleanup;
                            }
                        }
                        double fee = pos_trade->exit_scaled[0] + pos_trade->exit_scaled[1] + pos_trade->exit_scaled[2];
                        cost += fee;
                        fill_wealth += chg - fee;
                    }
                    pos_trade = NULL;
                }
            }
            if (pos_trade != NULL) {
                funding_owner = pos_trade;
            }
        }

        while (next_trade_cursor < n_trades && specs[next_trade_cursor].entry_idx < i) {
            next_trade_cursor++;
        }
        TradeSpec *candidate = (next_trade_cursor < n_trades && specs[next_trade_cursor].entry_idx == i)
                                   ? &specs[next_trade_cursor]
                                   : NULL;
        if (candidate != NULL && is_entry_elig) {
            if (pos_trade != NULL) {
                Py_DECREF(ledger);
                Py_DECREF(accepted_out);
                PyMem_Free(prices); PyMem_Free(spreads); PyMem_Free(points); PyMem_Free(posted_units);
                PyMem_Free(elig); PyMem_Free(entry_elig); PyMem_Free(exit_elig); PyMem_Free(specs);
                return Py_BuildValue("(sO)", "overlap", Py_None);
            }
            double budget = fill_wealth;
            if (budget > initial) budget = initial;
            if (budget < 0.0) budget = 0.0;
            Py_ssize_t q = 0;
            if (!paused) {
                if (calc_max_quantity(budget, candidate->entry_margin, candidate->ec, stress, native_spread, &q) < 0) {
                    goto cleanup;
                }
            }
            if (q > 0) {
                candidate->quantity = q;
                if (exact_round2(candidate->entry_margin * (double)q, &candidate->margin_rub) < 0) goto cleanup;
                candidate->gross_rub = 0.0;
                for (int k = 0; k < 3; k++) {
                    if (exact_round2(candidate->ec[k] * (double)q, &candidate->entry_scaled[k]) < 0) goto cleanup;
                }
                double fee = candidate->entry_scaled[0] + candidate->entry_scaled[1] + candidate->entry_scaled[2];
                cost += fee;

                PyObject *idx_obj = PyLong_FromSsize_t((Py_ssize_t)(next_trade_cursor));
                if (!idx_obj || PyList_Append(accepted_out, idx_obj) < 0) {
                    Py_XDECREF(idx_obj);
                    goto cleanup;
                }
                Py_DECREF(idx_obj);

                pos_trade = candidate;
                pos_mark = candidate->entry_spread_fill;
                funding_owner = candidate;
                int same_day_exit = (candidate->exit_idx == i);
                if (is_elig) {
                    double end_val = same_day_exit ? candidate->exit_spread_fill : spreads[i];
                    double chg;
                    if (exact_round2(candidate->sign * (end_val - pos_mark) * points[i] * (double)q, &chg) < 0) {
                        goto cleanup;
                    }
                    candidate->gross_rub += chg;
                    pos_mark = end_val;
                    daily += chg;
                } else {
                    pos_mark = candidate->entry_spread_fill;
                }
                if (same_day_exit) {
                    if (candidate->open_at_end && funding_only) {
                        candidate->has_exit_costs = 0;
                    } else {
                        candidate->has_exit_costs = 1;
                        for (int k = 0; k < 3; k++) {
                            if (exact_round2(candidate->xc[k] * (double)q, &candidate->exit_scaled[k]) < 0) goto cleanup;
                        }
                        cost += candidate->exit_scaled[0] + candidate->exit_scaled[1] + candidate->exit_scaled[2];
                    }
                    pos_trade = NULL;
                }
            }
        }

        Py_ssize_t funding_q = (funding_owner != NULL && unit_funding_contracts) ? funding_owner->quantity : 0;
        double funding;
        if (exact_round2(unit_funding * (double)funding_q, &funding) < 0) goto cleanup;
        if (funding_owner != NULL) {
            funding_owner->gross_rub += funding;
        }
        if (exact_round2(daily + funding, &daily) < 0) goto cleanup;
        if (exact_round2(cost, &cost) < 0) goto cleanup;
        if (exact_round2(cumulative + daily - cost, &cumulative) < 0) goto cleanup;
        double growth = (i > 0) ? (prices[i] / prices[i - 1]) : 1.0;
        wealth = free_cash * growth + posted + daily - cost;
        if (pos_trade == NULL) {
            posted = 0.0;
        } else if (is_elig || is_entry_elig) {
            posted = posted_units[i] * (double)pos_trade->quantity;
        }
        free_cash = wealth - posted;
        int shortfall = (free_cash < -1e-7);
        double excess = wealth - initial * prices[i] / p0;
        if (excess > peak) peak = excess;
        /* Only a cash shortfall stops new entries: without free cash no margin
           can be posted. Benchmark-relative drawdown limits no longer pause the
           book, matching the Python fallback. */
        paused |= shortfall;

        PyObject *entry = _PyDict_NewPresized(4);
        PyObject *cum_obj = PyFloat_FromDouble(cumulative);
        PyObject *w_obj = PyFloat_FromDouble(wealth);
        PyObject *sf_obj = shortfall ? Py_True : Py_False;
        if (!entry || !cum_obj || !w_obj) {
            Py_XDECREF(entry); Py_XDECREF(cum_obj); Py_XDECREF(w_obj);
            goto cleanup;
        }
        Py_INCREF(sf_obj);
        PyObject *date_obj = PyList_GET_ITEM(dates_lst, i);
        if (PyDict_SetItem(entry, k_date, date_obj) < 0 ||
            PyDict_SetItem(entry, k_cumulative_net_rub, cum_obj) < 0 ||
            PyDict_SetItem(entry, k_account_value_rub, w_obj) < 0 ||
            PyDict_SetItem(entry, k_cash_shortfall, sf_obj) < 0) {
            Py_DECREF(entry); Py_DECREF(cum_obj); Py_DECREF(w_obj); Py_DECREF(sf_obj);
            goto cleanup;
        }
        Py_DECREF(cum_obj); Py_DECREF(w_obj); Py_DECREF(sf_obj);
        PyList_SET_ITEM(ledger, i, entry);
    }

    Py_ssize_t n_acc = PyList_GET_SIZE(accepted_out);
    PyObject *acc_tuples = PyList_New(n_acc);
    if (!acc_tuples) goto cleanup;
    for (Py_ssize_t a = 0; a < n_acc; a++) {
        Py_ssize_t sp_idx = PyLong_AsSsize_t(PyList_GET_ITEM(accepted_out, a));
        TradeSpec *sp = &specs[sp_idx];
        double gross_rounded;
        if (exact_round2(sp->gross_rub, &gross_rounded) < 0) {
            Py_DECREF(acc_tuples);
            goto cleanup;
        }
        PyObject *tup = Py_BuildValue(
            "(nnddddddddi)",
            sp->orig_idx, sp->quantity, sp->margin_rub, gross_rounded,
            sp->entry_scaled[0], sp->entry_scaled[1], sp->entry_scaled[2],
            sp->exit_scaled[0], sp->exit_scaled[1], sp->exit_scaled[2],
            sp->has_exit_costs
        );
        if (!tup) {
            Py_DECREF(acc_tuples);
            goto cleanup;
        }
        PyList_SET_ITEM(acc_tuples, a, tup);
    }
    Py_DECREF(accepted_out);
    accepted_out = NULL;

    PyMem_Free(prices); PyMem_Free(spreads); PyMem_Free(points); PyMem_Free(posted_units);
    PyMem_Free(elig); PyMem_Free(entry_elig); PyMem_Free(exit_elig); PyMem_Free(specs);

    PyObject *ok = PyTuple_Pack(2, ledger, acc_tuples);
    Py_DECREF(ledger);
    Py_DECREF(acc_tuples);
    return ok;

cleanup:
    Py_XDECREF(ledger);
    Py_XDECREF(accepted_out);
    PyMem_Free(prices); PyMem_Free(spreads); PyMem_Free(points); PyMem_Free(posted_units);
    PyMem_Free(elig); PyMem_Free(entry_elig); PyMem_Free(exit_elig); PyMem_Free(specs);
    return NULL;
}

static PyMethodDef FastCoreMethods[] = {
    {"compute_rolling_z", fast_compute_rolling_z, METH_VARARGS, "Exact population rolling z-score."},
    {"clean_obj", fast_clean_obj, METH_O, "Recursively replace non-finite floats with None."},
    {"mean_reversion_scan", fast_mean_reversion_scan, METH_VARARGS, "Fast mean reversion state machine scan."},
    {"size_proof_metrics", fast_size_proof_metrics, METH_VARARGS, "Exact metrics_only max-affordable sizing scan."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef fastcore_module = {
    PyModuleDef_HEAD_INIT,
    "_fastcore",
    "Optional C99 accelerator for exact rolling z-score, payload cleaning, MR scan, and sizing.",
    -1,
    FastCoreMethods
};

PyMODINIT_FUNC PyInit__fastcore(void) {
    return PyModule_Create(&fastcore_module);
}
