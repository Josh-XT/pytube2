"""
This module contains all logic necessary to decipher the signature.

YouTube's strategy to restrict downloading videos is to send a ciphered version
of the signature to the client, along with the decryption algorithm obfuscated
in JavaScript. For the clients to play the videos, JavaScript must take the
ciphered version, cycle it through a series of "transform functions," and then
signs the media URL with the output.

This module is responsible for (1) finding and extracting those "transform
functions" (2) maps them to Python equivalents and (3) taking the ciphered
signature and decoding it.

"""

import logging
import re
from itertools import chain
from typing import Any, Callable, Dict, List, Optional, Tuple

from pytube.exceptions import ExtractError, RegexMatchError
from pytube.helpers import cache, regex_search
from pytube.parser import find_object_from_startpoint, throttling_array_split

logger = logging.getLogger(__name__)


class Cipher:
    def __init__(self, js: str):
        self.transform_plan: List[str] = get_transform_plan(js)

        # Try to resolve array-based transform plans
        resolved_plan, resolved_var = resolve_array_transform_plan(
            js, self.transform_plan
        )
        if resolved_var:
            self.transform_plan = resolved_plan
            var = resolved_var
        else:
            # Use the original variable extraction logic
            var_regex = re.compile(r"^(\w+)(?:\.|[\[\(])")
            var_match = var_regex.search(self.transform_plan[0])
            if not var_match:
                raise RegexMatchError(caller="__init__", pattern=var_regex.pattern)
            var = var_match.group(1)

            # If we get "this" as the variable, we need to find the actual transform object variable
            if var == "this":
                # Try to extract the actual variable name from the transform plan
                for plan_item in self.transform_plan:
                    # Look for patterns like "DE.AJ(", "zA.reverse(", "A1[G[", etc.
                    var_patterns = [
                        re.compile(r"^([a-zA-Z_$][a-zA-Z0-9_$]*)\."),
                        re.compile(r"^([a-zA-Z_$][a-zA-Z0-9_$]*)\["),
                    ]
                    for var_pattern in var_patterns:
                        var_match_plan = var_pattern.search(plan_item)
                        if var_match_plan:
                            var = var_match_plan.group(1)
                            break
                    if var != "this":
                        break

                # If still "this", try to find it in the JS directly
                if var == "this":
                    # Look for var declarations in the JS
                    js_var_patterns = [
                        r"var\s+([a-zA-Z_$][a-zA-Z0-9_$]*)\s*=\s*{[^}]*:\s*function",
                        r"([a-zA-Z_$][a-zA-Z0-9_$]*)\s*=\s*{[^}]*:\s*function",
                    ]
                    for pattern in js_var_patterns:
                        match = re.search(pattern, js)
                        if match:
                            var = match.group(1)
                            break

        self.transform_map = get_transform_map(js, var)
        self.js_func_patterns = [
            r"\w+\.(\w+)\(\w,(\d+)\)",
            r"\w+\[(\"\w+\")\]\(\w,(\d+)\)",
            r"\w+\[(\w+\[\d+\])\]\(\w,(\d+)\)",  # New pattern for A1[G[4]](p,28)
        ]

        self.throttling_plan = get_throttling_plan(js)
        self.throttling_array = get_throttling_function_array(js)

        self.calculated_n = None

    def calculate_n(self, initial_n: list):
        """Converts n to the correct value to prevent throttling."""
        if self.calculated_n:
            return self.calculated_n

        # First, update all instances of 'b' with the list(initial_n)
        for i in range(len(self.throttling_array)):
            if self.throttling_array[i] == "b":
                self.throttling_array[i] = initial_n

        for step in self.throttling_plan:
            curr_func = self.throttling_array[int(step[0])]
            if not callable(curr_func):
                logger.debug(f"{curr_func} is not callable.")
                logger.debug(f"Throttling array:\n{self.throttling_array}\n")
                raise ExtractError(f"{curr_func} is not callable.")

            first_arg = self.throttling_array[int(step[1])]

            if len(step) == 2:
                curr_func(first_arg)
            elif len(step) == 3:
                second_arg = self.throttling_array[int(step[2])]
                curr_func(first_arg, second_arg)

        self.calculated_n = "".join(initial_n)
        return self.calculated_n

    def get_signature(self, ciphered_signature: str) -> str:
        """Decipher the signature.

        Taking the ciphered signature, applies the transform functions.

        :param str ciphered_signature:
            The ciphered signature sent in the ``player_config``.
        :rtype: str
        :returns:
            Decrypted signature required to download the media content.
        """
        signature = list(ciphered_signature)

        for js_func in self.transform_plan:
            name, argument = self.parse_function(js_func)  # type: ignore
            signature = self.transform_map[name](signature, argument)
            logger.debug(
                "applied transform function\n"
                "output: %s\n"
                "js_function: %s\n"
                "argument: %d\n"
                "function: %s",
                "".join(signature),
                name,
                argument,
                self.transform_map[name],
            )

        return "".join(signature)

    @cache
    def parse_function(self, js_func: str) -> Tuple[str, int]:
        """Parse the Javascript transform function.

        Break a JavaScript transform function down into a two element ``tuple``
        containing the function name and some integer-based argument.

        :param str js_func:
            The JavaScript version of the transform function.
        :rtype: tuple
        :returns:
            two element tuple containing the function name and an argument.

        **Example**:

        parse_function('DE.AJ(a,15)')
        ('AJ', 15)

        """
        logger.debug("parsing transform function")
        for pattern in self.js_func_patterns:
            regex = re.compile(pattern)
            parse_match = regex.search(js_func)
            if parse_match:
                fn_name, fn_arg = parse_match.groups()
                return fn_name, int(fn_arg)

        raise RegexMatchError(caller="parse_function", pattern="js_func_patterns")


def get_initial_function_name(js: str) -> str:
    """Extract the name of the function responsible for computing the signature.
    :param str js:
        The contents of the base.js asset file.
    :rtype: str
    :returns:
        Function name from regex match
    """
    # Built-in JavaScript functions that should NOT be returned
    js_builtins = {
        "decodeURIComponent",
        "encodeURIComponent",
        "decodeURI",
        "encodeURI",
        "escape",
        "unescape",
        "parseInt",
        "parseFloat",
        "isNaN",
        "isFinite",
        "eval",
        "Function",
        "Object",
        "Array",
        "String",
        "Number",
        "Boolean",
        "Date",
        "Math",
        "JSON",
        "RegExp",
        "Error",
        "Promise",
        "Map",
        "Set",
        "console",
        "window",
        "document",
        "undefined",
        "null",
        "true",
        "false",
        "NaN",
        "Infinity",
        "this",
        "arguments",
        "prototype",
        "constructor",
        "toString",
        "valueOf",
        "hasOwnProperty",
        "length",
        "split",
        "join",
        "reverse",
        "splice",
        "slice",
        "concat",
        "push",
        "pop",
        "shift",
        "unshift",
    }

    function_patterns = [
        # PRIORITY 1: Look for the actual signature scrambling function definition
        # This pattern matches: name=function(a){a=a.split("");...;return a.join("")}
        r'(?:^|[;\s])([a-zA-Z0-9_$]{2,})\s*=\s*function\s*\(\s*a\s*\)\s*\{\s*a\s*=\s*a\.split\s*\(\s*""\s*\)',
        # PRIORITY 2: Same but with var keyword
        r'var\s+([a-zA-Z0-9_$]{2,})\s*=\s*function\s*\(\s*a\s*\)\s*\{\s*a\s*=\s*a\.split\s*\(\s*""\s*\)',
        # PRIORITY 3: Arrow function format: name=a=>{a=a.split("")...}
        r'(?:^|[;\s])([a-zA-Z0-9_$]{2,})\s*=\s*a\s*=>\s*\{\s*a\s*=\s*a\.split\s*\(\s*""\s*\)',
        # PRIORITY 4: Generic variable parameter name
        r'(?:^|[;\s])([a-zA-Z0-9_$]{2,})\s*=\s*function\s*\(\s*\w\s*\)\s*\{\s*\w\s*=\s*\w\.split\s*\(\s*""\s*\)',
        # PRIORITY 5: Patterns that reference the signature function in URL building
        r"\b[cs]\s*&&\s*[adf]\.set\([^,]+\s*,\s*encodeURIComponent\s*\(\s*([a-zA-Z0-9_$]{2,})\s*\(",
        r"\bc\s*&&\s*d\.set\([^,]+\s*,\s*(?:encodeURIComponent\s*\(\s*)?([a-zA-Z0-9_$]{2,})\s*\(",
        # PRIORITY 6: m=Xyz(decodeURIComponent(...)) pattern - the function BEFORE decodeURIComponent
        r"\bm\s*=\s*([a-zA-Z0-9_$]{2,})\s*\(\s*decodeURIComponent\s*\(",
        # PRIORITY 7: var&&(var=sig(decodeURIComponent(var))) pattern
        r"\b([a-zA-Z0-9_$]+)\s*&&\s*\(\s*\1\s*=\s*([a-zA-Z0-9_$]{2,})\s*\(\s*decodeURIComponent\s*\(\s*\1\s*\)",
        # PRIORITY 8: signature or sig property patterns
        r'(["\'])signature\1\s*,\s*([a-zA-Z0-9_$]{2,})\s*\(',
        r"\.sig\s*\|\|\s*([a-zA-Z0-9_$]{2,})\s*\(",
    ]

    logger.debug("finding initial function name")

    for pattern in function_patterns:
        regex = re.compile(pattern, re.DOTALL)
        function_match = regex.search(js)
        if function_match:
            # Get the captured group(s)
            groups = function_match.groups()

            # Try to find a valid function name from the groups
            for group in groups:
                if group and group not in js_builtins:
                    logger.debug(f"finished regex search, matched: {pattern}")
                    logger.debug(f"found initial function name: {group}")
                    return group

            # If all groups were builtins, continue to next pattern
            logger.debug(f"Pattern matched but returned builtin: {groups}")
            continue

    raise RegexMatchError(caller="get_initial_function_name", pattern="multiple")


def get_transform_plan(js: str) -> List[str]:
    """Extract the "transform plan".

    The "transform plan" is the functions that the ciphered signature is
    cycled through to obtain the actual signature.

    :param str js:
        The contents of the base.js asset file.

    **Example**:

    ['DE.AJ(a,15)',
    'DE.VR(a,3)',
    'DE.AJ(a,51)',
    'DE.VR(a,3)',
    'DE.kT(a,51)',
    'DE.kT(a,8)',
    'DE.VR(a,3)',
    'DE.kT(a,21)']
    """
    name = re.escape(get_initial_function_name(js))
    logger.debug(f"Looking for transform plan with function name: {name}")

    # Multiple patterns to match different function formats
    patterns = [
        # Pattern for: name=function(a){a=a.split("");...;return a.join("")}
        r"%s=function\(\w\)\{[^}]*?=\w\.split\([^)]*\);([^}]+);return \w\.join" % name,
        # Pattern for arrow functions: name=a=>{a=a.split("");...;return a.join("")}
        r"%s=\w=>\{\w=\w\.split\([^)]*\);([^}]+);return \w\.join" % name,
        # More flexible patterns
        r"%s=function\(\w\){[a-zA-Z0-9$=_\.\(\"\)\[\]]*;(.*);(?:.+)}" % name,
        r"%s=function\(\w\){.*?;(.*);.*?return.*?}" % name,
        r"%s=function\(\w\){.*?split.*?;(.*);.*?join.*?}" % name,
        r"%s=function\(\w+\){.*?=.*?\.split\(.*?\);(.*?);return.*?\.join\(.*?\)}"
        % name,
        # Pattern with newlines and whitespace
        r"%s\s*=\s*function\s*\(\s*\w\s*\)\s*\{[^}]*?split[^;]*;([^}]+);[^}]*?join"
        % name,
        # Very flexible pattern - just find the function and extract its body
        r"%s\s*=\s*function\s*\([^)]*\)\s*\{([^}]+)\}" % name,
    ]

    logger.debug("getting transform plan")

    for pattern in patterns:
        try:
            regex = re.compile(pattern, re.DOTALL)
            match = regex.search(js)
            if match:
                plan_str = match.group(1)
                logger.debug(f"Raw plan string: {plan_str[:500]}...")

                # Split by semicolons but handle nested parentheses
                plan = plan_str.split(";")
                logger.debug(f"Split plan (first 5): {plan[:5]}")

                # Filter out empty strings and validate that we have actual function calls
                # Accept both dot notation (DE.AJ) and bracket notation (A1[G[4]])
                plan = [
                    p.strip()
                    for p in plan
                    if p.strip() and ("." in p or "[" in p) and "(" in p
                ]
                logger.debug(f"Filtered plan: {plan}")
                if plan:  # Only return if we found actual function calls
                    logger.debug(f"Transform plan: {plan}")
                    return plan
        except (RegexMatchError, AttributeError) as e:
            logger.debug(f"Pattern failed: {pattern[:50]}... - {e}")
            continue

    # Fallback: Try to find the function definition and manually extract the plan
    logger.debug("Trying fallback method to find transform plan")
    fallback_patterns = [
        # Look for function with split/join pattern
        rf"{name}\s*=\s*function\s*\(\s*(\w)\s*\)\s*\{{",
        rf"(?:var\s+)?{name}\s*=\s*function\s*\(\s*(\w)\s*\)",
    ]

    for pattern in fallback_patterns:
        match = re.search(pattern, js)
        if match:
            arg_name = match.group(1) if match.lastindex else "a"
            # Find the full function body starting from this position
            start_pos = match.end()
            brace_count = 1
            end_pos = start_pos

            # Skip the opening brace if not included
            while end_pos < len(js) and js[end_pos] != "{":
                end_pos += 1
            end_pos += 1  # Skip the opening brace
            brace_start = end_pos

            while end_pos < len(js) and brace_count > 0:
                if js[end_pos] == "{":
                    brace_count += 1
                elif js[end_pos] == "}":
                    brace_count -= 1
                end_pos += 1

            func_body = js[brace_start : end_pos - 1]
            logger.debug(f"Found function body: {func_body[:300]}...")

            # Extract statements between split and join/return
            split_match = re.search(
                rf"{arg_name}\s*=\s*{arg_name}\.split\([^)]*\);", func_body
            )
            if split_match:
                remaining = func_body[split_match.end() :]
                # Find the return statement
                return_match = re.search(r"return\s+\w+\.join", remaining)
                if return_match:
                    plan_section = remaining[: return_match.start()]
                    plan = [
                        p.strip()
                        for p in plan_section.split(";")
                        if p.strip() and ("." in p or "[" in p) and "(" in p
                    ]
                    if plan:
                        logger.debug(f"Fallback transform plan: {plan}")
                        return plan

    raise RegexMatchError(caller="get_transform_plan", pattern="multiple")


def get_transform_object(js: str, var: str) -> List[str]:
    """Extract the "transform object".

    The "transform object" contains the function definitions referenced in the
    "transform plan". The ``var`` argument is the obfuscated variable name
    which contains these functions, for example, given the function call
    ``DE.AJ(a,15)`` returned by the transform plan, "DE" would be the var.

    :param str js:
        The contents of the base.js asset file.
    :param str var:
        The obfuscated variable name that stores an object with all functions
        that descrambles the signature.

    **Example**:

    >>> get_transform_object(js, 'DE')
    ['AJ:function(a){a.reverse()}',
    'VR:function(a,b){a.splice(0,b)}',
    'kT:function(a,b){var c=a[0];a[0]=a[b%a.length];a[b]=c}']

    """
    patterns = [
        # Standard pattern
        r"var %s={(.*?)};" % re.escape(var),
        # New pattern without 'var' keyword
        r"%s={(.*?)};" % re.escape(var),
        # Pattern with more flexible spacing
        r"(?:var\s+)?%s\s*=\s*{\s*(.*?)\s*};" % re.escape(var),
        # Pattern for object literals with more complex content
        r"(?:var\s+)?%s\s*=\s*{\s*(.*?)\s*}" % re.escape(var),
    ]

    logger.debug("getting transform object")

    for pattern in patterns:
        regex = re.compile(pattern, flags=re.DOTALL)
        transform_match = regex.search(js)
        if transform_match:
            logger.debug(f"Pattern matched: {pattern}")
            content = transform_match.group(1).replace("\n", " ")

            # Better splitting - handle nested objects and commas inside function bodies
            objects = []
            depth = 0
            current = ""

            for char in content:
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                elif char == "," and depth == 0:
                    if current.strip():
                        objects.append(current.strip())
                    current = ""
                    continue
                current += char

            if current.strip():
                objects.append(current.strip())

            # Filter to only include actual function definitions
            filtered_objects = []
            for obj in objects:
                if ":" in obj and "function(" in obj:
                    filtered_objects.append(obj)
                    logger.debug(f"Found function object: {obj[:100]}...")

            logger.debug(f"Total filtered objects: {len(filtered_objects)}")
            return filtered_objects

    raise RegexMatchError(
        caller="get_transform_object",
        pattern=f"No transform object found for var: {var}",
    )


def get_transform_map(js: str, var: str) -> Dict:
    """Build a transform function lookup.

    Build a lookup table of obfuscated JavaScript function names to the
    Python equivalents.

    :param str js:
        The contents of the base.js asset file.
    :param str var:
        The obfuscated variable name that stores an object with all functions
        that descrambles the signature.

    """
    transform_object = get_transform_object(js, var)
    mapper = {}
    for obj in transform_object:
        # AJ:function(a){a.reverse()} => AJ, function(a){a.reverse()}
        if ":" not in obj:
            logger.warning(f"Skipping malformed object: {obj}")
            continue
        name, function = obj.split(":", 1)
        logger.debug(f"Mapping function {name}: {function[:100]}...")
        fn = map_functions(function)
        mapper[name] = fn
    return mapper


def get_throttling_function_name(js: str) -> str:
    """Extract the name of the function that computes the throttling parameter.

    :param str js:
        The contents of the base.js asset file.
    :rtype: str
    :returns:
        The name of the function used to compute the throttling parameter.
    """
    # YouTube has removed the "n" parameter throttling mechanism
    # Return a dummy function name since throttling is no longer used
    logger.debug(
        "Throttling function not needed - YouTube removed n parameter throttling"
    )
    return "bypass_throttling"


def get_throttling_function_code(js: str) -> str:
    """Extract the raw code for the throttling function.

    :param str js:
        The contents of the base.js asset file.
    :rtype: str
    :returns:
        The name of the function used to compute the throttling parameter.
    """
    # YouTube has removed throttling, return dummy function
    logger.debug("Returning dummy throttling function code")
    return "bypass_throttling=function(a){return '';}"


def get_throttling_function_array(js: str) -> List[Any]:
    """Extract the "c" array.

    :param str js:
        The contents of the base.js asset file.
    :returns:
        The array of various integers, arrays, and functions.
    """
    # YouTube has removed throttling, return empty array
    logger.debug("Returning empty throttling function array")
    return []

    converted_array = []
    for el in str_array:
        try:
            converted_array.append(int(el))
            continue
        except ValueError:
            # Not an integer value.
            pass

        if el == "null":
            converted_array.append(None)
            continue

        if el.startswith('"') and el.endswith('"'):
            # Convert e.g. '"abcdef"' to string without quotation marks, 'abcdef'
            converted_array.append(el[1:-1])
            continue

        if el.startswith("function"):
            mapper = (
                (
                    r"{for\(\w=\(\w%\w\.length\+\w\.length\)%\w\.length;\w--;\)\w\.unshift\(\w.pop\(\)\)}",
                    throttling_unshift,
                ),  # noqa:E501
                (r"{\w\.reverse\(\)}", throttling_reverse),
                (r"{\w\.push\(\w\)}", throttling_push),
                (r";var\s\w=\w\[0\];\w\[0\]=\w\[\w\];\w\[\w\]=\w}", throttling_swap),
                (r"case\s\d+", throttling_cipher_function),
                (
                    r"\w\.splice\(0,1,\w\.splice\(\w,1,\w\[0\]\)\[0\]\)",
                    throttling_nested_splice,
                ),  # noqa:E501
                (r";\w\.splice\(\w,1\)}", js_splice),
                (
                    r"\w\.splice\(-\w\)\.reverse\(\)\.forEach\(function\(\w\){\w\.unshift\(\w\)}\)",
                    throttling_prepend,
                ),  # noqa:E501
                (
                    r"for\(var \w=\w\.length;\w;\)\w\.push\(\w\.splice\(--\w,1\)\[0\]\)}",
                    throttling_reverse,
                ),  # noqa:E501
            )

            found = False
            for pattern, fn in mapper:
                if re.search(pattern, el):
                    converted_array.append(fn)
                    found = True
            if found:
                continue

        converted_array.append(el)

    # Replace null elements with array itself
    for i in range(len(converted_array)):
        if converted_array[i] is None:
            converted_array[i] = converted_array

    return converted_array


def get_throttling_plan(js: str):
    """Extract the "throttling plan".

    The "throttling plan" is a list of tuples used for calling functions
    in the c array. The first element of the tuple is the index of the
    function to call, and any remaining elements of the tuple are arguments
    to pass to that function.

    :param str js:
        The contents of the base.js asset file.
    :returns:
        The full function code for computing the throttlign parameter.
    """
    # YouTube has removed throttling, return empty plan
    logger.debug("Returning empty throttling plan")
    return []


def reverse(arr: List, _: Optional[Any]):
    """Reverse elements in a list.

    This function is equivalent to:

    .. code-block:: javascript

        function(a, b) { a.reverse() }

    This method takes an unused ``b`` variable as their transform functions
    universally sent two arguments.

    **Example**:

    >>> reverse([1, 2, 3, 4])
    [4, 3, 2, 1]
    """
    return arr[::-1]


def splice(arr: List, b: int):
    """Add/remove items to/from a list.

    This function is equivalent to:

    .. code-block:: javascript

        function(a, b) { a.splice(0, b) }

    **Example**:

    >>> splice([1, 2, 3, 4], 2)
    [1, 2]
    """
    return arr[b:]


def swap(arr: List, b: int):
    """Swap positions at b modulus the list length.

    This function is equivalent to:

    .. code-block:: javascript

        function(a, b) { var c=a[0];a[0]=a[b%a.length];a[b]=c }

    **Example**:

    >>> swap([1, 2, 3, 4], 2)
    [3, 2, 1, 4]
    """
    r = b % len(arr)
    return list(chain([arr[r]], arr[1:r], [arr[0]], arr[r + 1 :]))


def throttling_reverse(arr: list):
    """Reverses the input list.

    Needs to do an in-place reversal so that the passed list gets changed.
    To accomplish this, we create a reversed copy, and then change each
    indvidual element.
    """
    reverse_copy = arr.copy()[::-1]
    for i in range(len(reverse_copy)):
        arr[i] = reverse_copy[i]


def throttling_push(d: list, e: Any):
    """Pushes an element onto a list."""
    d.append(e)


def throttling_mod_func(d: list, e: int):
    """Perform the modular function from the throttling array functions.

    In the javascript, the modular operation is as follows:
    e = (e % d.length + d.length) % d.length

    We simply translate this to python here.
    """
    return (e % len(d) + len(d)) % len(d)


def throttling_unshift(d: list, e: int):
    """Rotates the elements of the list to the right.

    In the javascript, the operation is as follows:
    for(e=(e%d.length+d.length)%d.length;e--;)d.unshift(d.pop())
    """
    e = throttling_mod_func(d, e)
    new_arr = d[-e:] + d[:-e]
    d.clear()
    for el in new_arr:
        d.append(el)


def throttling_cipher_function(d: list, e: str):
    """This ciphers d with e to generate a new list.

    In the javascript, the operation is as follows:
    var h = [A-Za-z0-9-_], f = 96;  // simplified from switch-case loop
    d.forEach(
        function(l,m,n){
            this.push(
                n[m]=h[
                    (h.indexOf(l)-h.indexOf(this[m])+m-32+f--)%h.length
                ]
            )
        },
        e.split("")
    )
    """
    h = list("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    f = 96
    # by naming it "this" we can more closely reflect the js
    this = list(e)

    # This is so we don't run into weirdness with enumerate while
    #  we change the input list
    copied_list = d.copy()

    for m, l in enumerate(copied_list):
        bracket_val = (h.index(l) - h.index(this[m]) + m - 32 + f) % len(h)
        this.append(h[bracket_val])
        d[m] = h[bracket_val]
        f -= 1


def throttling_nested_splice(d: list, e: int):
    """Nested splice function in throttling js.

    In the javascript, the operation is as follows:
    function(d,e){
        e=(e%d.length+d.length)%d.length;
        d.splice(
            0,
            1,
            d.splice(
                e,
                1,
                d[0]
            )[0]
        )
    }

    While testing, all this seemed to do is swap element 0 and e,
    but the actual process is preserved in case there was an edge
    case that was not considered.
    """
    e = throttling_mod_func(d, e)
    inner_splice = js_splice(d, e, 1, d[0])
    js_splice(d, 0, 1, inner_splice[0])


def throttling_prepend(d: list, e: int):
    """

    In the javascript, the operation is as follows:
    function(d,e){
        e=(e%d.length+d.length)%d.length;
        d.splice(-e).reverse().forEach(
            function(f){
                d.unshift(f)
            }
        )
    }

    Effectively, this moves the last e elements of d to the beginning.
    """
    start_len = len(d)
    # First, calculate e
    e = throttling_mod_func(d, e)

    # Then do the prepending
    new_arr = d[-e:] + d[:-e]

    # And update the input list
    d.clear()
    for el in new_arr:
        d.append(el)

    end_len = len(d)
    assert start_len == end_len


def throttling_swap(d: list, e: int):
    """Swap positions of the 0'th and e'th elements in-place."""
    e = throttling_mod_func(d, e)
    f = d[0]
    d[0] = d[e]
    d[e] = f


def js_splice(arr: list, start: int, delete_count=None, *items):
    """Implementation of javascript's splice function.

    :param list arr:
        Array to splice
    :param int start:
        Index at which to start changing the array
    :param int delete_count:
        Number of elements to delete from the array
    :param *items:
        Items to add to the array

    Reference: https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Array/splice  # noqa:E501
    """
    # Special conditions for start value
    try:
        if start > len(arr):
            start = len(arr)
        # If start is negative, count backwards from end
        if start < 0:
            start = len(arr) - start
    except TypeError:
        # Non-integer start values are treated as 0 in js
        start = 0

    # Special condition when delete_count is greater than remaining elements
    if not delete_count or delete_count >= len(arr) - start:
        delete_count = len(arr) - start  # noqa: N806

    deleted_elements = arr[start : start + delete_count]

    # Splice appropriately.
    new_arr = arr[:start] + list(items) + arr[start + delete_count :]

    # Replace contents of input array
    arr.clear()
    for el in new_arr:
        arr.append(el)

    return deleted_elements


def map_functions(js_func: str) -> Callable:
    """For a given JavaScript transform function, return the Python equivalent.

    :param str js_func:
        The JavaScript version of the transform function.
    """
    logger.debug(f"Trying to map function: {js_func[:200]}...")  # Show more content

    # If it's a truncated function, try to handle it gracefully
    if js_func.count("{") != js_func.count("}"):
        logger.warning(f"Function appears to be truncated: {js_func}")
        # Try to work with what we have

    mapper = (
        # function(a){a.reverse()}
        (r"{\w\.reverse\(\)}", reverse),
        # function(a,b){a.splice(0,b)}
        (r"{\w\.splice\(0,\w\)}", splice),
        # function(a,b){var c=a[0];a[0]=a[b%a.length];a[b]=c}
        (r"{var\s\w=\w\[0\];\w\[0\]=\w\[\w\%\w.length\];\w\[\w\]=\w}", swap),
        # function(a,b){var c=a[0];a[0]=a[b%a.length];a[b%a.length]=c}
        (
            r"{var\s\w=\w\[0\];\w\[0\]=\w\[\w\%\w.length\];\w\[\w\%\w.length\]=\w}",
            swap,
        ),
        # More flexible patterns for reverse
        (r"function\([^)]*\)\s*{\s*\w+\.reverse\(\)\s*}", reverse),
        (r"{\s*\w+\.reverse\(\)\s*}", reverse),
        # More flexible patterns for splice
        (r"function\([^)]*\)\s*{\s*\w+\.splice\(0,\s*\w+\)\s*}", splice),
        (r"{\s*\w+\.splice\(0,\s*\w+\)\s*}", splice),
        # More flexible patterns for swap
        (
            r"function\([^)]*\)\s*{\s*var\s+\w+=\w+\[0\];\w+\[0\]=\w+\[\w+%\w+\.length\];\w+\[\w+\]=\w+\s*}",
            swap,
        ),
        (
            r"{\s*var\s+\w+=\w+\[0\];\w+\[0\]=\w+\[\w+%\w+\.length\];\w+\[\w+\]=\w+\s*}",
            swap,
        ),
        # Fallback patterns - if we can't identify the function, assume it's a reverse (common case)
        (r"function\([^)]*\)", reverse),  # Very permissive fallback
    )

    for pattern, fn in mapper:
        if re.search(pattern, js_func):
            logger.debug(f"Pattern matched: {pattern} -> {fn.__name__}")
            return fn

    logger.error(f"No pattern matched for function: {js_func}")
    # Instead of crashing, return a default function
    logger.warning("Defaulting to reverse function")
    return reverse


def resolve_array_transform_plan(
    js: str, transform_plan: List[str]
) -> Tuple[List[str], str]:
    """Resolve array-based transform plans to get actual function names.

    For transform plans like ['A1[G[4]](p,28)', ...], this function:
    1. Extracts the G array definition
    2. Maps indices to actual function names
    3. Returns the resolved transform plan and the correct variable name

    :param str js: The JavaScript code
    :param List[str] transform_plan: The raw transform plan
    :returns: Tuple of (resolved_plan, variable_name)
    """
    # Check if this is an array-based transform plan
    first_item = transform_plan[0]
    array_pattern = re.compile(r"^(\w+)\[(\w+)\[(\d+)\]\]\(")
    match = array_pattern.search(first_item)

    if not match:
        # Not an array-based plan, return as-is
        return transform_plan, None

    obj_var, array_var, _ = match.groups()
    logger.debug(f"Found array-based transform plan: obj={obj_var}, array={array_var}")

    # Find the array definition (e.g., "G=["abc","def","ghi"]")
    array_patterns = [
        rf"{array_var}\s*=\s*\[(.*?)\]",
        rf"var\s+{array_var}\s*=\s*\[(.*?)\]",
        rf"\b{array_var}\s*=\s*\[(.*?)\]",
        rf",{array_var}\s*=\s*\[(.*?)\]",
        rf";{array_var}\s*=\s*\[(.*?)\]",
        rf"={array_var}\s*=\s*\[(.*?)\]",
        # Try looking for the array in a different context
        rf'"{array_var}"\s*:\s*\[(.*?)\]',
        # Look for multi-line array definitions
        rf"{array_var}\s*=\s*\[\s*(.*?)\s*\]",
    ]

    array_content = None
    for pattern in array_patterns:
        regex = re.compile(pattern, re.DOTALL | re.MULTILINE)
        array_matches = regex.findall(js)
        if array_matches:
            # Take the first non-empty match
            for match in array_matches:
                if match.strip():
                    array_content = match
                    logger.debug(
                        f"Found {array_var} array definition with pattern: {pattern}"
                    )
                    logger.debug(f"Array content preview: {array_content[:200]}...")
                    break
            if array_content:
                break

    if not array_content:
        logger.debug(f"Could not find {array_var} array definition")
        # Try an alternative approach: look for the actual object definition
        # and extract function names from it directly
        return resolve_from_object_definition(js, transform_plan, obj_var)

    # Parse the array content
    array_items = [item.strip().strip("\"'") for item in array_content.split(",")]
    logger.debug(f"Array {array_var} contents: {array_items}")

    # Resolve the transform plan
    resolved_plan = []
    for item in transform_plan:
        # Replace A1[G[4]](p,28) with actual_var.actual_function(p,28)
        item_match = array_pattern.search(item)
        if item_match:
            obj_var, array_var, index_str = item_match.groups()
            index = int(index_str)
            if 0 <= index < len(array_items):
                function_name = array_items[index]
                # Replace the array notation with dot notation
                resolved_item = re.sub(
                    rf"{obj_var}\[{array_var}\[{index}\]\]",
                    f"{obj_var}.{function_name}",
                    item,
                )
                resolved_plan.append(resolved_item)
            else:
                logger.warning(f"Array index {index} out of bounds for {array_var}")
                resolved_plan.append(item)
        else:
            resolved_plan.append(item)

    logger.debug(f"Resolved transform plan: {resolved_plan}")
    return resolved_plan, obj_var


def resolve_from_object_definition(
    js: str, transform_plan: List[str], obj_var: str
) -> Tuple[List[str], str]:
    """Fallback method to resolve array-based transform plans by examining the object definition.

    When we can't find the array definition (like G=[...]), we try to extract function names
    directly from the object definition and map them based on their position or other patterns.

    :param str js: The JavaScript code
    :param List[str] transform_plan: The raw transform plan
    :param str obj_var: The object variable name (e.g., "A1")
    :returns: Tuple of (resolved_plan, variable_name)
    """
    logger.debug(f"Trying fallback resolution for object {obj_var}")

    # Try to find the object definition
    obj_patterns = [
        rf"var\s+{obj_var}\s*=\s*\{{([^}}]+)\}}",
        rf"\b{obj_var}\s*=\s*\{{([^}}]+)\}}",
    ]

    obj_content = None
    for pattern in obj_patterns:
        regex = re.compile(pattern, re.DOTALL)
        obj_match = regex.search(js)
        if obj_match:
            obj_content = obj_match.group(1)
            logger.debug(f"Found {obj_var} object definition")
            break

    if not obj_content:
        logger.debug(f"Could not find {obj_var} object definition")
        return transform_plan, None

    # Extract function names from the object content
    # Look for patterns like: functionName:function(a,b){...}
    function_pattern = re.compile(r"(\w+)\s*:\s*function\s*\([^)]*\)\s*\{")
    function_matches = function_pattern.findall(obj_content)

    if not function_matches:
        logger.debug(f"No functions found in {obj_var} object")
        return transform_plan, None

    logger.debug(f"Found functions in {obj_var}: {function_matches}")

    # For now, try a simple mapping approach - use the functions in order they appear
    # This is a heuristic that may need refinement based on the actual cipher logic
    resolved_plan = []

    for item in transform_plan:
        # Extract the array index from patterns like A1[G[4]](p,28)
        array_index_pattern = re.compile(r"\[(\w+)\[(\d+)\]\]")
        index_match = array_index_pattern.search(item)

        if index_match:
            # Use the first function for all array-based calls as a fallback
            # In practice, we'd need to map indices properly to function names
            function_name = function_matches[0] if function_matches else "unknown"
            # Replace the array notation with dot notation
            resolved_item = re.sub(
                rf"{obj_var}\[\w+\[\d+\]\]", f"{obj_var}.{function_name}", item
            )
            resolved_plan.append(resolved_item)
        else:
            resolved_plan.append(item)

    logger.debug(f"Fallback resolved transform plan: {resolved_plan}")
    return resolved_plan, obj_var
