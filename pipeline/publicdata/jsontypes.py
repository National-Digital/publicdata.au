"""The type of a parsed JSON value, for data whose keys vary from one value to the next.

A stable shape gets a TypedDict of its own; these name the rest.
"""

type JSON = str | int | float | bool | list[JSON] | dict[str, JSON] | None
type JSONObject = dict[str, JSON]
