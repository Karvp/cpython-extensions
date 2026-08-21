from __future__ import annotations

import dis
import unittest

import python_extensions as pe


class PackageTests(unittest.TestCase):
    def test_exports(self):
        self.assertEqual(pe.__version__, "1.0.3")
        self.assertTrue(callable(pe.enable_switch))
        self.assertTrue(callable(pe.inline_function))
        self.assertTrue(callable(pe.enable_goto))

    def test_switch_python_keys(self):
        source = '''\
def classify(value):
    with switch(value):
        if case(1):
            return "number"
        if case():
            return "other"
'''
        ns = {"switch": pe.switch, "case": pe.case}
        exec(compile(source, "<test-switch-python>", "exec"), ns)
        fn = pe.enable_switch(source=source, case_key_mode="python")(ns["classify"])
        self.assertEqual(fn(1), "number")
        self.assertEqual(fn(True), "number")
        self.assertEqual(fn(1.0), "number")

    def test_switch_typed_keys(self):
        source = '''\
def classify(value):
    with switch(value):
        if case(1):
            return "int"
        if case(1.0):
            return "float"
        if case(True):
            return "bool"
        if case():
            return "other"
'''
        ns = {"switch": pe.switch, "case": pe.case}
        exec(compile(source, "<test-switch-typed>", "exec"), ns)
        fn = pe.enable_switch(source=source, case_key_mode="typed")(ns["classify"])
        self.assertEqual(fn(1), "int")
        self.assertEqual(fn(1.0), "float")
        self.assertEqual(fn(True), "bool")

    def test_inline_removes_call(self):
        pe.clear_inline_registry()
        namespace = {
            "inline_function": pe.inline_function,
            "inline_calls": pe.inline_calls,
            "__name__": __name__,
        }
        exec(
            "@inline_function(register_only=True)\n"
            "def add(a, b):\n"
            "    return a + b\n\n"
            "@inline_calls\n"
            "def merged(a, b):\n"
            "    return add(a, b)\n",
            namespace,
        )
        merged = namespace["merged"]
        self.assertEqual(merged(10, 20), 30)
        names = [i.opname for i in dis.get_instructions(merged)]
        self.assertNotIn("CALL", names)

    def test_compatibility_modules(self):
        import pyswitch
        import inline_function
        import pygoto

        self.assertIs(pyswitch.enable_switch, pe.enable_switch)
        self.assertIs(inline_function.inline_calls, pe.inline_calls)
        self.assertIs(pygoto.enable_goto, pe.enable_goto)


if __name__ == "__main__":
    unittest.main()



def test_goto_public_error_exports_are_complete():
    import python_extensions as pe
    from python_extensions.goto import GotoSyntaxError, UnsupportedGotoRuntimeError

    assert pe.GotoSyntaxError is GotoSyntaxError
    assert pe.UnsupportedGotoRuntimeError is UnsupportedGotoRuntimeError
