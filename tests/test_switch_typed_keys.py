import unittest
from pyswitch import enable_switch, switch, case, DuplicateCaseError


class CaseKeyModeTests(unittest.TestCase):
    def test_python_mode_collides(self):
        with self.assertRaises(DuplicateCaseError):
            @enable_switch(case_key_mode="python", mode="portable")
            def classify(x):
                with switch(x):
                    if case(1):
                        return "int"
                    if case(1.0):
                        return "float"
                    if case(True):
                        return "bool"
                    if case():
                        return "default"

    def test_typed_mode_distinguishes(self):
        @enable_switch(case_key_mode="typed", mode="portable")
        def classify(x):
            with switch(x):
                if case(1):
                    return "int"
                if case(1.0):
                    return "float"
                if case(True):
                    return "bool"
                if case():
                    return "default"

        self.assertEqual(classify(1), "int")
        self.assertEqual(classify(1.0), "float")
        self.assertEqual(classify(True), "bool")
        self.assertEqual(classify(False), "default")
        self.assertEqual(classify.__pyswitch_case_key_mode__, "typed")

    def test_default_python_behavior(self):
        @enable_switch(mode="portable")
        def classify(x):
            with switch(x):
                if case(1):
                    return "one"
                if case():
                    return "default"

        self.assertEqual(classify(True), "one")
        self.assertEqual(classify(1.0), "one")

    def test_typed_mode_same_type_still_collides(self):
        with self.assertRaises(DuplicateCaseError):
            @enable_switch(case_key_mode="typed", mode="portable")
            def classify(x):
                with switch(x):
                    if case(1):
                        return "a"
                    if case(1):
                        return "b"

    def test_invalid_mode(self):
        with self.assertRaises(ValueError):
            enable_switch(case_key_mode="strictish")

    def test_typed_general_body(self):
        @enable_switch(case_key_mode="typed", mode="portable")
        def classify(x):
            result = []
            with switch(x):
                if case(1):
                    result.append("int")
                if case(1.0):
                    result.append("float")
                if case(True):
                    result.append("bool")
                if case():
                    result.append("default")
            return result[0]

        self.assertEqual(classify(1), "int")
        self.assertEqual(classify(1.0), "float")
        self.assertEqual(classify(True), "bool")


if __name__ == '__main__':
    unittest.main()

class LiveTypedModeTests(unittest.TestCase):
    def test_fast_live_typed_mode(self):
        @enable_switch(case_key_mode="typed", mode="fast")
        def classify(x):
            with switch(x):
                if case(1):
                    return "int"
                if case(1.0):
                    return "float"
                if case(True):
                    return "bool"
                if case():
                    return "default"

        self.assertEqual(classify(1), "int")
        self.assertEqual(classify(1.0), "float")
        self.assertEqual(classify(True), "bool")
        self.assertEqual(classify(False), "default")
        self.assertEqual(classify.__pyswitch_case_key_mode__, "typed")
