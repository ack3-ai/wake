"""Exception types defined by the `wake_rs` extension must be importable and picklable.

`pyo3::create_exception!` stringifies its first argument into the type's module
name, so passing a string literal stringifies it a second time and the quotes end
up inside `__module__` - `'"wake.development.core"'`. Such a class cannot be
resolved by import, so `pickle` refuses it.

That is not cosmetic here: the multiprocess test plugin ships failures to the
coordinator by pickling `(type, value, traceback)`, and its fallback for
unpicklable *values* still pickles the same *type*. Both paths raise, so an
exception with a malformed module name replaces a real test failure with a
`PicklingError` from inside the error-reporting path.

Asserted findings:

* `__module__` is a plain, importable module name, and the class is reachable from
  it under `__qualname__`.
* Instances survive a pickle round trip, both directly and in the
  `(type, value, traceback)` shape the multiprocess plugin uses.
"""

import importlib
import pickle

import pytest

import wake_rs

EXCEPTIONS = ["HistoryPrunedError", "AbiError"]


@pytest.mark.parametrize("name", EXCEPTIONS)
def test_module_name_is_importable(name):
    exception = getattr(wake_rs, name)
    module = exception.__module__

    assert '"' not in module and "'" not in module, f"malformed module: {module!r}"
    assert getattr(importlib.import_module(module), exception.__qualname__) is exception


@pytest.mark.parametrize("name", EXCEPTIONS)
def test_instances_round_trip_through_pickle(name):
    exception = getattr(wake_rs, name)

    restored = pickle.loads(pickle.dumps(exception("boom")))
    assert type(restored) is exception
    assert str(restored) == "boom"

    # The shape `pytest_plugin_multiprocess` puts on the queue, and the fallback it
    # uses when the value itself will not pickle.
    pickle.loads(pickle.dumps((exception, exception("boom"), None)))
    pickle.loads(pickle.dumps((exception, Exception("boom"), None)))


@pytest.mark.parametrize("name", EXCEPTIONS)
def test_also_reachable_from_the_development_api(name):
    core = importlib.import_module("wake.development.core")
    assert getattr(core, name) is getattr(wake_rs, name)
