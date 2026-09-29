"""loads_lenient: the planner's JSON reader for model output.

A plan carries generated Python inside a JSON string. A model writing that
Python escapes for Python, not for JSON — a regex, a quoted apostrophe, a
Windows path — and ``json.loads`` then rejects the whole plan with
"Invalid escape" (the door-counter snapshot pipeline failed exactly so).
The reader repairs those backslashes without changing the Python the model
wrote, accepts raw newlines inside strings, and still raises on real garbage.
"""

# pylint: disable=missing-function-docstring,missing-class-docstring

import json
import unittest

from wactorz.agents.planner.parsing import extract_json_array, loads_lenient


class LoadsLenientTest(unittest.TestCase):
    def test_valid_json_is_returned_unchanged(self):
        text = '{"a": 1, "b": [true, null, "x"]}'
        self.assertEqual(loads_lenient(text), json.loads(text))

    def test_valid_escapes_are_left_alone(self):
        # Every escape JSON defines must survive exactly as json.loads reads it.
        text = r'{"s": "tab\t nl\n quote\" slash\\ solidus\/ bs\b ff\f cr\r u\u00e9"}'
        self.assertEqual(loads_lenient(text), json.loads(text))

    def test_regex_backslash_is_repaired_to_the_python_the_model_wrote(self):
        # The model wrote `re.search(r'\d+', s)` and escaped the backslash for
        # Python only: `\n` is a JSON escape, `\d` is not.
        text = r'{"code": "import re\nm = re.search(r' + "'" + r"\d+" + "'" + r', s)"}'
        with self.assertRaises(json.JSONDecodeError):
            json.loads(text)  # the failure being fixed
        self.assertEqual(loads_lenient(text)["code"], "import re\nm = re.search(r'\\d+', s)")

    def test_python_escaped_quote_is_repaired(self):
        text = '{"code": "print(' + "\\'" + "hi" + "\\'" + ')"}'
        self.assertEqual(loads_lenient(text)["code"], "print(\\'hi\\')")

    def test_windows_path_is_repaired(self):
        text = r'{"code": "p = \'C:\Users\pk\snap.jpg\'"}'
        self.assertEqual(loads_lenient(text)["code"], r"p = \'C:\Users\pk\snap.jpg\'")

    def test_raw_newline_inside_a_string_is_accepted(self):
        text = '{"code": "line one\nline two"}'
        with self.assertRaises(json.JSONDecodeError):
            json.loads(text)
        self.assertEqual(loads_lenient(text)["code"], "line one\nline two")

    def test_real_garbage_still_raises_the_original_error(self):
        with self.assertRaises(json.JSONDecodeError):
            loads_lenient('{"a": }')

    def test_repair_that_does_not_help_raises_the_original_error(self):
        # An invalid escape AND a structural error: the escape is repaired,
        # the structure is not, and the caller sees the failure json.loads
        # reported on the text as the model wrote it.
        text = r'{"code": "\d", "b": }'
        with self.assertRaises(json.JSONDecodeError) as ctx:
            loads_lenient(text)
        self.assertIn("Invalid", str(ctx.exception))

    def test_fenced_plan_with_regex_code_round_trips(self):
        response = (
            "```json\n"
            r'[{"name": "door-snapshot", "description": "x", '
            r'"spawn_config": {"type": "dynamic", '
            r'"code": "import re\nif re.match(r\"\d+\", s): pass"}}]'
            "\n```"
        )
        plan = loads_lenient(extract_json_array(response))
        self.assertEqual(plan[0]["name"], "door-snapshot")
        self.assertIn(r're.match(r"\d+", s)', plan[0]["spawn_config"]["code"])


if __name__ == "__main__":
    unittest.main()
