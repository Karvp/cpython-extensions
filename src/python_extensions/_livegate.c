#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <limits.h>
#include <stdint.h>
#include <string.h>

#if defined(_MSC_VER)
#  define LIVEGATE_ALWAYS_INLINE __forceinline
#elif defined(__GNUC__) || defined(__clang__)
#  define LIVEGATE_ALWAYS_INLINE inline __attribute__((always_inline))
#else
#  define LIVEGATE_ALWAYS_INLINE inline
#endif

/*
 * Optional CPython 3.13 accelerator for python_extensions.switch.
 *
 * The Python live backend historically executed two C operations through
 * Python bytecode on every dispatch: bound dict.get(...) followed by a ctypes
 * scalar STORE_ATTR.  This helper fuses exact-dict lookup and the raw gate
 * store into one METH_O built-in call.  It intentionally uses only the regular
 * (non-limited) CPython C API and standard C memory operations; the private
 * code-object address is still discovered and runtime-validated by switch.py.
 */

typedef struct {
    PyObject_HEAD
    PyObject *table;                 /* exact dict, immutable after construction */
    uintptr_t gate_address;          /* bound after final code-object creation */
    uint64_t default_encoded;
    uint64_t last_encoded;
    uint64_t *dense_values;
    unsigned char *dense_present;
    long long *int_hash_keys;
    uint64_t *int_hash_values;
    unsigned char *int_hash_used;
    PyTypeObject **typed_fast_types;
    PyObject **typed_fast_tables;
    long long dense_min;
    long long dense_max;
    Py_ssize_t dense_span;
    Py_ssize_t int_hash_capacity;
    Py_ssize_t typed_fast_count;
    int gate_width;                  /* code units: 1, 2, or 4 */
    unsigned char typed;
    unsigned char gate_bound;
    unsigned char have_last;
    unsigned char elide_writes;
    unsigned char dense_kind;        /* 0 none, 1 full span, 2 bitmap, 3 int hash */
    unsigned char int_has_huge;
} LiveDispatcherObject;

static PyTypeObject LiveDispatcherType;

static int
valid_width(int width)
{
    return width == 1 || width == 2 || width == 4;
}

static uint64_t
max_encoded_for_width(int width)
{
    if (width == 1) {
        return UINT16_MAX;
    }
    if (width == 2) {
        return UINT32_MAX;
    }
    return UINT64_MAX;
}

static int
py_long_to_u64(PyObject *value, uint64_t *result)
{
    unsigned long long encoded;
    if (!PyLong_Check(value)) {
        PyErr_SetString(PyExc_TypeError, "live jump-table values must be integers");
        return -1;
    }
    encoded = PyLong_AsUnsignedLongLong(value);
    if (encoded == ULLONG_MAX && PyErr_Occurred()) {
        return -1;
    }
    *result = (uint64_t)encoded;
    return 0;
}

static int
exact_int_to_ll(PyObject *value, long long *result)
{
    int overflow = 0;
    if (PyUnstable_Long_IsCompact((PyLongObject *)value)) {
        *result = (long long)PyUnstable_Long_CompactValue((PyLongObject *)value);
        return 1;
    }
    long long converted = PyLong_AsLongLongAndOverflow(value, &overflow);
    if (converted == -1 && PyErr_Occurred()) {
        return -1;
    }
    if (overflow != 0) {
        return 0;
    }
    *result = converted;
    return 1;
}

static void
clear_dense(LiveDispatcherObject *self)
{
    PyMem_Free(self->dense_values);
    PyMem_Free(self->dense_present);
    PyMem_Free(self->int_hash_keys);
    PyMem_Free(self->int_hash_values);
    PyMem_Free(self->int_hash_used);
    self->dense_values = NULL;
    self->dense_present = NULL;
    self->int_hash_keys = NULL;
    self->int_hash_values = NULL;
    self->int_hash_used = NULL;
    self->dense_min = 0;
    self->dense_max = 0;
    self->dense_span = 0;
    self->int_hash_capacity = 0;
    self->dense_kind = 0;
    self->int_has_huge = 0;
}

static void
clear_typed_fast(LiveDispatcherObject *self)
{
    Py_ssize_t index;
    if (self->typed_fast_tables != NULL) {
        for (index = 0; index < self->typed_fast_count; index++) {
            Py_XDECREF(self->typed_fast_tables[index]);
        }
    }
    PyMem_Free(self->typed_fast_types);
    PyMem_Free(self->typed_fast_tables);
    self->typed_fast_types = NULL;
    self->typed_fast_tables = NULL;
    self->typed_fast_count = 0;
}

static int
safe_builtin_typed_type(PyTypeObject *type)
{
    return type == &PyLong_Type ||
           type == &PyBool_Type ||
           type == &PyUnicode_Type ||
           type == &PyBytes_Type ||
           type == &PyFloat_Type ||
           type == &PyComplex_Type ||
           type == Py_TYPE(Py_None);
}

static int
build_typed_fast_partitions(LiveDispatcherObject *self)
{
    Py_ssize_t max_count;
    PyObject *key;
    PyObject *encoded;
    Py_ssize_t pos = 0;

    if (!self->typed) {
        return 0;
    }
    max_count = PyDict_Size(self->table);
    if (max_count <= 0) {
        return 0;
    }
    self->typed_fast_types = PyMem_Calloc(
        (size_t)max_count, sizeof(PyTypeObject *)
    );
    self->typed_fast_tables = PyMem_Calloc((size_t)max_count, sizeof(PyObject *));
    if (self->typed_fast_types == NULL || self->typed_fast_tables == NULL) {
        clear_typed_fast(self);
        PyErr_NoMemory();
        return -1;
    }

    while (PyDict_Next(self->table, &pos, &key, &encoded)) {
        PyObject *type_obj;
        PyObject *value_key;
        PyTypeObject *case_type;
        Py_ssize_t index;
        PyObject *subtable;

        if (!PyTuple_CheckExact(key) || PyTuple_GET_SIZE(key) != 2) {
            continue;
        }
        type_obj = PyTuple_GET_ITEM(key, 0);
        value_key = PyTuple_GET_ITEM(key, 1);
        if (!PyType_Check(type_obj)) {
            continue;
        }
        case_type = (PyTypeObject *)type_obj;
        if (!safe_builtin_typed_type(case_type) || Py_TYPE(value_key) != case_type) {
            continue;
        }

        for (index = 0; index < self->typed_fast_count; index++) {
            if (self->typed_fast_types[index] == case_type) {
                break;
            }
        }
        if (index == self->typed_fast_count) {
            subtable = PyDict_New();
            if (subtable == NULL) {
                clear_typed_fast(self);
                return -1;
            }
            self->typed_fast_types[index] = case_type;
            self->typed_fast_tables[index] = subtable;
            self->typed_fast_count++;
        }
        else {
            subtable = self->typed_fast_tables[index];
        }

        /* Rehashing is intentionally restricted to exact builtin values whose
           hash/equality implementations cannot invoke user Python code. */
        if (PyDict_SetItem(subtable, value_key, encoded) < 0) {
            clear_typed_fast(self);
            return -1;
        }
    }
    return 0;
}

static uint64_t
hash_i64(long long value)
{
    /* SplitMix64 finalizer.  This is only an internal routing hash; Python's
       observable hash/equality semantics have already been proven irrelevant
       before this lane is enabled. */
    uint64_t x = (uint64_t)value;
    x ^= x >> 30;
    x *= UINT64_C(0xbf58476d1ce4e5b9);
    x ^= x >> 27;
    x *= UINT64_C(0x94d049bb133111eb);
    x ^= x >> 31;
    return x;
}

static int
next_hash_capacity(Py_ssize_t count, Py_ssize_t *capacity_out)
{
    Py_ssize_t capacity = 8;
    if (count <= 0 || count > PY_SSIZE_T_MAX / 2) {
        return 0;
    }
    while (capacity < count * 2) {
        if (capacity > PY_SSIZE_T_MAX / 2) {
            return 0;
        }
        capacity <<= 1;
    }
    *capacity_out = capacity;
    return 1;
}

static int
range_span_with_limit(long long min_value, long long max_value,
                      Py_ssize_t limit, Py_ssize_t *span_out)
{
    long long max_allowed;
    if (limit <= 0 || min_value > max_value) {
        return 0;
    }

    /* Prove the signed subtraction below is small before evaluating it. */
    if ((unsigned long long)(limit - 1) > (unsigned long long)LLONG_MAX) {
        max_allowed = LLONG_MAX;
    }
    else if (min_value > LLONG_MAX - (long long)(limit - 1)) {
        max_allowed = LLONG_MAX;
    }
    else {
        max_allowed = min_value + (long long)(limit - 1);
    }
    if (max_value > max_allowed) {
        return 0;
    }
    *span_out = (Py_ssize_t)(max_value - min_value) + 1;
    return 1;
}

static int
scan_dense_int_candidates(LiveDispatcherObject *self,
                          Py_ssize_t *candidate_count,
                          long long *min_value,
                          long long *max_value,
                          int *all_keys_exact_int,
                          int *has_huge_int)
{
    PyObject *key;
    PyObject *value;
    Py_ssize_t pos = 0;
    Py_ssize_t count = 0;
    int first = 1;
    int all_int = 1;
    int huge = 0;

    while (PyDict_Next(self->table, &pos, &key, &value)) {
        PyObject *int_key = NULL;
        if (!self->typed) {
            if (PyLong_CheckExact(key)) {
                int_key = key;
            }
            else {
                all_int = 0;
            }
        }
        else if (PyTuple_CheckExact(key) && PyTuple_GET_SIZE(key) == 2 &&
                 PyTuple_GET_ITEM(key, 0) == (PyObject *)&PyLong_Type &&
                 PyLong_CheckExact(PyTuple_GET_ITEM(key, 1))) {
            int_key = PyTuple_GET_ITEM(key, 1);
        }

        if (int_key != NULL) {
            long long converted;
            int status = exact_int_to_ll(int_key, &converted);
            if (status < 0) {
                return -1;
            }
            if (status == 0) {
                /* Huge exact ints stay in the authoritative Python dict.  A
                   signed-64 selector cannot equal them, so fit selectors may
                   still use the native lane.  Overflowing selectors fall back
                   to the dict whenever this flag is present. */
                huge = 1;
                continue;
            }
            if (first) {
                *min_value = converted;
                *max_value = converted;
                first = 0;
            }
            else {
                if (converted < *min_value) {
                    *min_value = converted;
                }
                if (converted > *max_value) {
                    *max_value = converted;
                }
            }
            count++;
        }
    }

    *candidate_count = count;
    *all_keys_exact_int = all_int;
    *has_huge_int = huge;
    return 0;
}

static int
build_sparse_int_hash(LiveDispatcherObject *self, Py_ssize_t count)
{
    Py_ssize_t capacity;
    PyObject *key;
    PyObject *value;
    Py_ssize_t pos = 0;

    if (!next_hash_capacity(count, &capacity)) {
        return 0;
    }
    self->int_hash_keys = PyMem_Calloc((size_t)capacity, sizeof(long long));
    self->int_hash_values = PyMem_Calloc((size_t)capacity, sizeof(uint64_t));
    self->int_hash_used = PyMem_Calloc((size_t)capacity, sizeof(unsigned char));
    if (self->int_hash_keys == NULL || self->int_hash_values == NULL ||
        self->int_hash_used == NULL) {
        clear_dense(self);
        PyErr_NoMemory();
        return -1;
    }

    while (PyDict_Next(self->table, &pos, &key, &value)) {
        PyObject *int_key = NULL;
        long long converted;
        int status;
        uint64_t encoded;
        Py_ssize_t index;

        if (!self->typed) {
            if (PyLong_CheckExact(key)) {
                int_key = key;
            }
        }
        else if (PyTuple_CheckExact(key) && PyTuple_GET_SIZE(key) == 2 &&
                 PyTuple_GET_ITEM(key, 0) == (PyObject *)&PyLong_Type &&
                 PyLong_CheckExact(PyTuple_GET_ITEM(key, 1))) {
            int_key = PyTuple_GET_ITEM(key, 1);
        }
        if (int_key == NULL) {
            continue;
        }
        status = exact_int_to_ll(int_key, &converted);
        if (status < 0) {
            clear_dense(self);
            return -1;
        }
        if (status == 0) {
            continue;
        }
        if (py_long_to_u64(value, &encoded) < 0) {
            clear_dense(self);
            return -1;
        }
        if (encoded > max_encoded_for_width(self->gate_width)) {
            clear_dense(self);
            PyErr_SetString(PyExc_OverflowError,
                            "encoded jump does not fit live gate width");
            return -1;
        }

        index = (Py_ssize_t)(hash_i64(converted) & (uint64_t)(capacity - 1));
        while (self->int_hash_used[index]) {
            if (self->int_hash_keys[index] == converted) {
                self->int_hash_values[index] = encoded;
                goto next_item;
            }
            index = (index + 1) & (capacity - 1);
        }
        self->int_hash_used[index] = 1;
        self->int_hash_keys[index] = converted;
        self->int_hash_values[index] = encoded;
next_item:
        ;
    }

    self->int_hash_capacity = capacity;
    self->dense_kind = 3;
    return 1;
}

static int
build_dense_int_lane(LiveDispatcherObject *self)
{
    Py_ssize_t count = 0;
    Py_ssize_t span = 0;
    long long min_value = 0;
    long long max_value = 0;
    int all_keys_exact_int = 0;
    int has_huge_int = 0;
    Py_ssize_t limit;

    if (scan_dense_int_candidates(
            self, &count, &min_value, &max_value,
            &all_keys_exact_int, &has_huge_int) < 0) {
        return -1;
    }

    /* In ordinary Python-key mode we may bypass dict hashing only when every
       case key is an exact int.  Otherwise values such as True or 1.0 may be
       dictionary-equal to an int selector.  Typed mode may optimize the exact
       int partition independently because type identity is part of the key. */
    if ((!self->typed && !all_keys_exact_int) || count < 4) {
        return 0;
    }
    self->int_has_huge = (unsigned char)(has_huge_int != 0);

    /* Keep the auxiliary lane bounded: at most 64 Ki entries and at most 4x
       the number of participating integer keys. */
    limit = count > 16384 ? 65536 : count * 4;
    if (limit > 65536) {
        limit = 65536;
    }
    if (!range_span_with_limit(min_value, max_value, limit, &span)) {
        return build_sparse_int_hash(self, count) < 0 ? -1 : 0;
    }

    self->dense_values = PyMem_Calloc((size_t)span, sizeof(uint64_t));
    if (self->dense_values == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    if (span != count) {
        self->dense_present = PyMem_Calloc((size_t)span, sizeof(unsigned char));
        if (self->dense_present == NULL) {
            clear_dense(self);
            PyErr_NoMemory();
            return -1;
        }
    }

    {
        PyObject *key;
        PyObject *value;
        Py_ssize_t pos = 0;
        while (PyDict_Next(self->table, &pos, &key, &value)) {
            PyObject *int_key = NULL;
            long long converted;
            int status;
            uint64_t encoded;
            Py_ssize_t index;

            if (!self->typed) {
                if (!PyLong_CheckExact(key)) {
                    continue;
                }
                int_key = key;
            }
            else if (PyTuple_CheckExact(key) && PyTuple_GET_SIZE(key) == 2 &&
                     PyTuple_GET_ITEM(key, 0) == (PyObject *)&PyLong_Type &&
                     PyLong_CheckExact(PyTuple_GET_ITEM(key, 1))) {
                int_key = PyTuple_GET_ITEM(key, 1);
            }
            else {
                continue;
            }

            status = exact_int_to_ll(int_key, &converted);
            if (status < 0) {
                clear_dense(self);
                return -1;
            }
            if (status == 0 || converted < min_value || converted > max_value) {
                continue;
            }
            if (py_long_to_u64(value, &encoded) < 0) {
                clear_dense(self);
                return -1;
            }
            if (encoded > max_encoded_for_width(self->gate_width)) {
                clear_dense(self);
                PyErr_SetString(PyExc_OverflowError,
                                "encoded jump does not fit live gate width");
                return -1;
            }
            index = (Py_ssize_t)(converted - min_value);
            self->dense_values[index] = encoded;
            if (self->dense_present != NULL) {
                self->dense_present[index] = 1;
            }
        }
    }

    self->dense_min = min_value;
    self->dense_max = max_value;
    self->dense_span = span;
    self->dense_kind = self->dense_present == NULL ? 1 : 2;
    return 0;
}

static int
write_gate(LiveDispatcherObject *self, uint64_t encoded)
{
    if (!self->gate_bound || self->gate_address == 0) {
        PyErr_SetString(PyExc_RuntimeError, "live dispatcher gate is not bound");
        return -1;
    }

    if (self->elide_writes && self->have_last && self->last_encoded == encoded) {
        return 0;
    }

    switch (self->gate_width) {
        case 1: {
            uint16_t value = (uint16_t)encoded;
            memcpy((void *)self->gate_address, &value, sizeof(value));
            break;
        }
        case 2: {
            uint32_t value = (uint32_t)encoded;
            memcpy((void *)self->gate_address, &value, sizeof(value));
            break;
        }
        case 4: {
            uint64_t value = encoded;
            memcpy((void *)self->gate_address, &value, sizeof(value));
            break;
        }
        default:
            PyErr_SetString(PyExc_RuntimeError, "invalid live gate width");
            return -1;
    }

    self->last_encoded = encoded;
    self->have_last = 1;
    return 0;
}

static LIVEGATE_ALWAYS_INLINE int
lookup_general(LiveDispatcherObject *self, PyObject *subject, uint64_t *encoded_out)
{
    PyObject *lookup_key = subject;
    PyObject *owned_key = NULL;
    PyObject *value;

    if (self->typed) {
        owned_key = PyTuple_New(2);
        if (owned_key == NULL) {
            return -1;
        }
        Py_INCREF((PyObject *)Py_TYPE(subject));
        PyTuple_SET_ITEM(owned_key, 0, (PyObject *)Py_TYPE(subject));
        Py_INCREF(subject);
        PyTuple_SET_ITEM(owned_key, 1, subject);
        lookup_key = owned_key;
    }

    value = PyDict_GetItemWithError(self->table, lookup_key);
    Py_XDECREF(owned_key);

    if (value == NULL) {
        if (PyErr_Occurred()) {
            /* Match switch.py's historical rule exactly: intrinsically
               unhashable selector types are ordinary misses; a TypeError from
               a real user __hash__ implementation propagates. */
            if (PyErr_ExceptionMatches(PyExc_TypeError) &&
                Py_TYPE(subject)->tp_hash == PyObject_HashNotImplemented) {
                PyErr_Clear();
                *encoded_out = self->default_encoded;
                return 0;
            }
            return -1;
        }
        *encoded_out = self->default_encoded;
        return 0;
    }

    if (py_long_to_u64(value, encoded_out) < 0) {
        return -1;
    }
    return 0;
}

static int
try_typed_fast(LiveDispatcherObject *self, PyObject *subject,
               uint64_t *encoded_out, int *handled)
{
    PyTypeObject *subject_type;
    Py_ssize_t index;
    PyObject *value;

    *handled = 0;
    if (!self->typed) {
        return 0;
    }
    subject_type = Py_TYPE(subject);
    if (!safe_builtin_typed_type(subject_type)) {
        return 0;
    }

    for (index = 0; index < self->typed_fast_count; index++) {
        if (self->typed_fast_types[index] != subject_type) {
            continue;
        }
        value = PyDict_GetItemWithError(self->typed_fast_tables[index], subject);
        if (value == NULL) {
            if (PyErr_Occurred()) {
                return -1;
            }
            *encoded_out = self->default_encoded;
        }
        else if (py_long_to_u64(value, encoded_out) < 0) {
            return -1;
        }
        *handled = 1;
        return 0;
    }

    /* For these exact builtin types both the type and value hashes are known
       non-observable C operations, so a missing type partition is a definite
       typed-key miss without constructing the historical tuple. */
    *encoded_out = self->default_encoded;
    *handled = 1;
    return 0;
}

static int
try_dense_int(LiveDispatcherObject *self, PyObject *subject,
              uint64_t *encoded_out, int *handled)
{
    long long selector;
    int status;
    Py_ssize_t index;

    *handled = 0;
    if (self->dense_kind == 0 || !PyLong_CheckExact(subject)) {
        return 0;
    }

    status = exact_int_to_ll(subject, &selector);
    if (status < 0) {
        return -1;
    }
    if (status == 0) {
        if (self->int_has_huge) {
            return 0;
        }
        *encoded_out = self->default_encoded;
        *handled = 1;
        return 0;
    }

    if (self->dense_kind == 3) {
        Py_ssize_t index = (Py_ssize_t)(
            hash_i64(selector) & (uint64_t)(self->int_hash_capacity - 1)
        );
        while (self->int_hash_used[index]) {
            if (self->int_hash_keys[index] == selector) {
                *encoded_out = self->int_hash_values[index];
                *handled = 1;
                return 0;
            }
            index = (index + 1) & (self->int_hash_capacity - 1);
        }
        *encoded_out = self->default_encoded;
        *handled = 1;
        return 0;
    }

    if (selector < self->dense_min || selector > self->dense_max) {
        *encoded_out = self->default_encoded;
        *handled = 1;
        return 0;
    }

    /* Construction guarantees dense_max-dense_min <= 65535, so this
       subtraction is now proven representable. */
    index = (Py_ssize_t)(selector - self->dense_min);
    if (self->dense_kind == 1 || self->dense_present[index]) {
        *encoded_out = self->dense_values[index];
    }
    else {
        *encoded_out = self->default_encoded;
    }
    *handled = 1;
    return 0;
}

static PyObject *
LiveDispatcher_dispatch(LiveDispatcherObject *self, PyObject *subject)
{
    uint64_t encoded;
    int handled = 0;

    if (try_dense_int(self, subject, &encoded, &handled) < 0) {
        return NULL;
    }
    if (!handled && try_typed_fast(self, subject, &encoded, &handled) < 0) {
        return NULL;
    }
    if (!handled && lookup_general(self, subject, &encoded) < 0) {
        return NULL;
    }
    if (write_gate(self, encoded) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}


/* Plain Python-key tables whose keys are all exact ints can bypass the
   generic typed/general routing tree for exact-int subjects.  Non-exact ints,
   bools, floats, and user subclasses still fall back to the authoritative
   dictionary lookup so Python hash/equality collision semantics are retained. */
static int
lookup_plain_int_lane(LiveDispatcherObject *self, PyObject *subject,
                      uint64_t *encoded_out)
{
    long long selector;
    int status;

    if (!PyLong_CheckExact(subject)) {
        return lookup_general(self, subject, encoded_out);
    }
    status = exact_int_to_ll(subject, &selector);
    if (status < 0) {
        return -1;
    }
    if (status == 0) {
        if (self->int_has_huge) {
            return lookup_general(self, subject, encoded_out);
        }
        *encoded_out = self->default_encoded;
        return 0;
    }

    if (self->dense_kind == 3) {
        Py_ssize_t index = (Py_ssize_t)(
            hash_i64(selector) & (uint64_t)(self->int_hash_capacity - 1)
        );
        while (self->int_hash_used[index]) {
            if (self->int_hash_keys[index] == selector) {
                *encoded_out = self->int_hash_values[index];
                return 0;
            }
            index = (index + 1) & (self->int_hash_capacity - 1);
        }
        *encoded_out = self->default_encoded;
        return 0;
    }

    if (selector < self->dense_min || selector > self->dense_max) {
        *encoded_out = self->default_encoded;
        return 0;
    }
    {
        Py_ssize_t index = (Py_ssize_t)(selector - self->dense_min);
        if (self->dense_kind == 1 || self->dense_present[index]) {
            *encoded_out = self->dense_values[index];
        }
        else {
            *encoded_out = self->default_encoded;
        }
    }
    return 0;
}

#define DEFINE_PLAIN_INT_FIXED_WIDTH_DISPATCH(NAME, CTYPE) \
static PyObject * \
NAME(LiveDispatcherObject *self, PyObject *subject) \
{ \
    uint64_t encoded; \
    uintptr_t address; \
    CTYPE value; \
    if (lookup_plain_int_lane(self, subject, &encoded) < 0) { \
        return NULL; \
    } \
    address = self->gate_address; \
    if (address == 0) { \
        PyErr_SetString(PyExc_RuntimeError, "live dispatcher gate is not bound"); \
        return NULL; \
    } \
    value = (CTYPE)encoded; \
    memcpy((void *)address, &value, sizeof(value)); \
    Py_RETURN_NONE; \
}

DEFINE_PLAIN_INT_FIXED_WIDTH_DISPATCH(LiveDispatcher_dispatch_plain_int_w1, uint16_t)
DEFINE_PLAIN_INT_FIXED_WIDTH_DISPATCH(LiveDispatcher_dispatch_plain_int_w2, uint32_t)
DEFINE_PLAIN_INT_FIXED_WIDTH_DISPATCH(LiveDispatcher_dispatch_plain_int_w4, uint64_t)


/* Gate-width-specialized generic entries retain the exact authoritative
   lookup sequence but avoid the generic write_gate state machine. */
#define DEFINE_GENERIC_FIXED_WIDTH_DISPATCH(NAME, CTYPE) \
static PyObject * \
NAME(LiveDispatcherObject *self, PyObject *subject) \
{ \
    uint64_t encoded; \
    int handled = 0; \
    uintptr_t address; \
    CTYPE value; \
    if (try_dense_int(self, subject, &encoded, &handled) < 0) { \
        return NULL; \
    } \
    if (!handled && try_typed_fast(self, subject, &encoded, &handled) < 0) { \
        return NULL; \
    } \
    if (!handled && lookup_general(self, subject, &encoded) < 0) { \
        return NULL; \
    } \
    address = self->gate_address; \
    if (address == 0) { \
        PyErr_SetString(PyExc_RuntimeError, "live dispatcher gate is not bound"); \
        return NULL; \
    } \
    value = (CTYPE)encoded; \
    memcpy((void *)address, &value, sizeof(value)); \
    Py_RETURN_NONE; \
}

DEFINE_GENERIC_FIXED_WIDTH_DISPATCH(LiveDispatcher_dispatch_generic_w1, uint16_t)
DEFINE_GENERIC_FIXED_WIDTH_DISPATCH(LiveDispatcher_dispatch_generic_w2, uint32_t)
DEFINE_GENERIC_FIXED_WIDTH_DISPATCH(LiveDispatcher_dispatch_generic_w4, uint64_t)

static void
LiveDispatcher_dealloc(LiveDispatcherObject *self)
{
    Py_XDECREF(self->table);
    clear_dense(self);
    clear_typed_fast(self);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyMethodDef LiveDispatcher_methods[] = {
    {"dispatch", (PyCFunction)LiveDispatcher_dispatch, METH_O,
     PyDoc_STR("dispatch(subject) -> None")},
    {NULL, NULL, 0, NULL},
};

static PyTypeObject LiveDispatcherType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "python_extensions._livegate._LiveDispatcher",
    .tp_basicsize = sizeof(LiveDispatcherObject),
    .tp_dealloc = (destructor)LiveDispatcher_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_methods = LiveDispatcher_methods,
};

static LiveDispatcherObject *
new_context(PyObject *table, int width, uint64_t default_encoded,
            int typed, int elide_writes, int dense_int, int typed_fast)
{
    LiveDispatcherObject *self;

    if (!PyDict_CheckExact(table)) {
        PyErr_SetString(PyExc_TypeError, "table must be an exact dict");
        return NULL;
    }
    if (!valid_width(width)) {
        PyErr_SetString(PyExc_ValueError, "width must be 1, 2, or 4 code units");
        return NULL;
    }
    if (default_encoded > max_encoded_for_width(width)) {
        PyErr_SetString(PyExc_OverflowError,
                        "default encoded jump does not fit gate width");
        return NULL;
    }

    self = PyObject_New(LiveDispatcherObject, &LiveDispatcherType);
    if (self == NULL) {
        return NULL;
    }
    self->table = NULL;
    self->gate_address = 0;
    self->default_encoded = default_encoded;
    self->last_encoded = 0;
    self->dense_values = NULL;
    self->dense_present = NULL;
    self->int_hash_keys = NULL;
    self->int_hash_values = NULL;
    self->int_hash_used = NULL;
    self->typed_fast_types = NULL;
    self->typed_fast_tables = NULL;
    self->dense_min = 0;
    self->dense_max = 0;
    self->dense_span = 0;
    self->int_hash_capacity = 0;
    self->typed_fast_count = 0;
    self->gate_width = width;
    self->typed = (unsigned char)(typed != 0);
    self->gate_bound = 0;
    self->have_last = 0;
    self->elide_writes = (unsigned char)(elide_writes != 0);
    self->dense_kind = 0;
    self->int_has_huge = 0;

    Py_INCREF(table);
    self->table = table;
    if (dense_int && build_dense_int_lane(self) < 0) {
        Py_DECREF(self);
        return NULL;
    }
    if (typed_fast && build_typed_fast_partitions(self) < 0) {
        Py_DECREF(self);
        return NULL;
    }
    return self;
}

static PyMethodDef dispatch_generic_w1_method = {
    "dispatch", (PyCFunction)LiveDispatcher_dispatch_generic_w1, METH_O,
    PyDoc_STR("dispatch(subject) -> None")
};
static PyMethodDef dispatch_generic_w2_method = {
    "dispatch", (PyCFunction)LiveDispatcher_dispatch_generic_w2, METH_O,
    PyDoc_STR("dispatch(subject) -> None")
};
static PyMethodDef dispatch_generic_w4_method = {
    "dispatch", (PyCFunction)LiveDispatcher_dispatch_generic_w4, METH_O,
    PyDoc_STR("dispatch(subject) -> None")
};

static PyMethodDef dispatch_plain_int_w1_method = {
    "dispatch", (PyCFunction)LiveDispatcher_dispatch_plain_int_w1, METH_O,
    PyDoc_STR("dispatch(subject) -> None")
};
static PyMethodDef dispatch_plain_int_w2_method = {
    "dispatch", (PyCFunction)LiveDispatcher_dispatch_plain_int_w2, METH_O,
    PyDoc_STR("dispatch(subject) -> None")
};
static PyMethodDef dispatch_plain_int_w4_method = {
    "dispatch", (PyCFunction)LiveDispatcher_dispatch_plain_int_w4, METH_O,
    PyDoc_STR("dispatch(subject) -> None")
};


static PyObject *
bound_dispatch_method(LiveDispatcherObject *self)
{
    PyMethodDef *methoddef;

    if (self->elide_writes) {
        return PyObject_GetAttrString((PyObject *)self, "dispatch");
    }
    if (!self->typed && self->dense_kind != 0) {
        if (self->gate_width == 1) methoddef = &dispatch_plain_int_w1_method;
        else if (self->gate_width == 2) methoddef = &dispatch_plain_int_w2_method;
        else methoddef = &dispatch_plain_int_w4_method;
    }
    else {
        if (self->gate_width == 1) methoddef = &dispatch_generic_w1_method;
        else if (self->gate_width == 2) methoddef = &dispatch_generic_w2_method;
        else methoddef = &dispatch_generic_w4_method;
    }
    return PyCFunction_New(methoddef, (PyObject *)self);
}

static LiveDispatcherObject *
context_from_dispatcher(PyObject *dispatcher)
{
    PyObject *self;
    if (!PyCFunction_Check(dispatcher)) {
        PyErr_SetString(PyExc_TypeError, "expected a native live dispatcher method");
        return NULL;
    }
    self = PyCFunction_GetSelf(dispatcher);
    if (self == NULL || !PyObject_TypeCheck(self, &LiveDispatcherType)) {
        PyErr_SetString(PyExc_TypeError, "expected a native live dispatcher method");
        return NULL;
    }
    return (LiveDispatcherObject *)self;
}

static PyObject *
module_make_dispatcher(PyObject *module, PyObject *args, PyObject *kwargs)
{
    PyObject *table;
    unsigned long long default_encoded;
    int width;
    int typed = 0;
    int elide_writes = 0;
    int dense_int = 1;
    LiveDispatcherObject *self;
    PyObject *method;
    static char *kwlist[] = {
        "table", "width", "default_encoded", "typed",
        "elide_writes", "dense_int", NULL
    };
    (void)module;

    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "OiK|ppp:make_dispatcher", kwlist,
            &table, &width, &default_encoded, &typed,
            &elide_writes, &dense_int)) {
        return NULL;
    }

    self = new_context(table, width, (uint64_t)default_encoded,
                       typed, elide_writes, dense_int, 1);
    if (self == NULL) {
        return NULL;
    }
    method = bound_dispatch_method(self);
    Py_DECREF(self);
    return method;
}

static PyObject *
module_bind_dispatcher(PyObject *module, PyObject *args)
{
    PyObject *dispatcher;
    unsigned long long address;
    LiveDispatcherObject *self;
    (void)module;

    if (!PyArg_ParseTuple(args, "OK:bind_dispatcher", &dispatcher, &address)) {
        return NULL;
    }
    if (address == 0) {
        PyErr_SetString(PyExc_ValueError, "gate address must be non-zero");
        return NULL;
    }
    self = context_from_dispatcher(dispatcher);
    if (self == NULL) {
        return NULL;
    }
    self->gate_address = (uintptr_t)address;
    self->gate_bound = 1;
    self->have_last = 0;
    Py_RETURN_NONE;
}

static PyObject *
module_clone_dispatcher(PyObject *module, PyObject *dispatcher)
{
    LiveDispatcherObject *source;
    LiveDispatcherObject *clone;
    PyObject *method;
    (void)module;

    source = context_from_dispatcher(dispatcher);
    if (source == NULL) {
        return NULL;
    }
    clone = new_context(source->table, source->gate_width,
                        source->default_encoded, source->typed,
                        source->elide_writes, 0, 0);
    if (clone == NULL) {
        return NULL;
    }

    /* Clone dense lanes without re-running Python-level hashing/equality. */
    if (source->dense_kind != 0) {
        if (source->dense_kind == 3) {
            size_t key_bytes = (size_t)source->int_hash_capacity * sizeof(long long);
            size_t value_bytes = (size_t)source->int_hash_capacity * sizeof(uint64_t);
            size_t used_bytes = (size_t)source->int_hash_capacity;
            clone->int_hash_keys = PyMem_Malloc(key_bytes);
            clone->int_hash_values = PyMem_Malloc(value_bytes);
            clone->int_hash_used = PyMem_Malloc(used_bytes);
            if (clone->int_hash_keys == NULL || clone->int_hash_values == NULL ||
                clone->int_hash_used == NULL) {
                Py_DECREF(clone);
                PyErr_NoMemory();
                return NULL;
            }
            memcpy(clone->int_hash_keys, source->int_hash_keys, key_bytes);
            memcpy(clone->int_hash_values, source->int_hash_values, value_bytes);
            memcpy(clone->int_hash_used, source->int_hash_used, used_bytes);
            clone->int_hash_capacity = source->int_hash_capacity;
        }
        else {
            size_t values_size = (size_t)source->dense_span * sizeof(uint64_t);
            clone->dense_values = PyMem_Malloc(values_size);
            if (clone->dense_values == NULL) {
                Py_DECREF(clone);
                PyErr_NoMemory();
                return NULL;
            }
            memcpy(clone->dense_values, source->dense_values, values_size);
            if (source->dense_present != NULL) {
                clone->dense_present = PyMem_Malloc((size_t)source->dense_span);
                if (clone->dense_present == NULL) {
                    Py_DECREF(clone);
                    PyErr_NoMemory();
                    return NULL;
                }
                memcpy(clone->dense_present, source->dense_present,
                       (size_t)source->dense_span);
            }
            clone->dense_min = source->dense_min;
            clone->dense_max = source->dense_max;
            clone->dense_span = source->dense_span;
        }
        clone->dense_kind = source->dense_kind;
        clone->int_has_huge = source->int_has_huge;
    }

    if (source->typed_fast_count != 0) {
        Py_ssize_t index;
        clone->typed_fast_types = PyMem_Malloc(
            (size_t)source->typed_fast_count * sizeof(PyTypeObject *)
        );
        clone->typed_fast_tables = PyMem_Calloc(
            (size_t)source->typed_fast_count, sizeof(PyObject *)
        );
        if (clone->typed_fast_types == NULL || clone->typed_fast_tables == NULL) {
            Py_DECREF(clone);
            PyErr_NoMemory();
            return NULL;
        }
        for (index = 0; index < source->typed_fast_count; index++) {
            clone->typed_fast_types[index] = source->typed_fast_types[index];
            clone->typed_fast_tables[index] = source->typed_fast_tables[index];
            Py_INCREF(clone->typed_fast_tables[index]);
            clone->typed_fast_count++;
        }
    }

    method = bound_dispatch_method(clone);
    Py_DECREF(clone);
    return method;
}

static PyObject *
module_dispatcher_info(PyObject *module, PyObject *dispatcher)
{
    LiveDispatcherObject *self;
    const char *dense = "none";
    PyObject *result;
    (void)module;

    self = context_from_dispatcher(dispatcher);
    if (self == NULL) {
        return NULL;
    }
    if (self->dense_kind == 1) {
        dense = "contiguous-int";
    }
    else if (self->dense_kind == 2) {
        dense = "dense-int";
    }
    else if (self->dense_kind == 3) {
        dense = "int-hash";
    }

    result = Py_BuildValue(
        "{s:s,s:i,s:O,s:O,s:s,s:L,s:n,s:n,s:n,s:O,s:O}",
        "engine", "native-fused-v1",
        "gate_width", self->gate_width,
        "typed", self->typed ? Py_True : Py_False,
        "bound", self->gate_bound ? Py_True : Py_False,
        "lookup_strategy", dense,
        "dense_min", self->dense_min,
        "dense_span", self->dense_span,
        "int_hash_capacity", self->int_hash_capacity,
        "typed_fast_partitions", self->typed_fast_count,
        "int_has_huge", self->int_has_huge ? Py_True : Py_False,
        "elide_writes", self->elide_writes ? Py_True : Py_False
    );
    return result;
}

static PyMethodDef module_methods[] = {
    {"make_dispatcher", (PyCFunction)(void(*)(void))module_make_dispatcher,
     METH_VARARGS | METH_KEYWORDS,
     PyDoc_STR("Create an unbound fused live dispatcher method.")},
    {"bind_dispatcher", module_bind_dispatcher, METH_VARARGS,
     PyDoc_STR("Bind a dispatcher to a verified live gate address.")},
    {"clone_dispatcher", module_clone_dispatcher, METH_O,
     PyDoc_STR("Clone a dispatcher without a gate binding.")},
    {"dispatcher_info", module_dispatcher_info, METH_O,
     PyDoc_STR("Return diagnostic information for a dispatcher.")},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef module_definition = {
    PyModuleDef_HEAD_INIT,
    "_livegate",
    "Optional native live-switch dispatch accelerator.",
    -1,
    module_methods,
};

PyMODINIT_FUNC
PyInit__livegate(void)
{
    PyObject *module;
    if (PyType_Ready(&LiveDispatcherType) < 0) {
        return NULL;
    }
    module = PyModule_Create(&module_definition);
    if (module == NULL) {
        return NULL;
    }
    if (PyModule_AddStringConstant(module, "ENGINE", "native-fused-v1") < 0) {
        Py_DECREF(module);
        return NULL;
    }
    return module;
}
