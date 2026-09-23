from lobstr_mcp.toon import to_toon


def test_scalars_and_object():
    out = to_toon({"count": 2, "ok": True, "missing": None, "name": "hi"})
    assert out == "count: 2\nok: true\nmissing: null\nname: hi"


def test_inline_scalar_array():
    out = to_toon({"available_fields": ["name", "phone", "email"]})
    assert out == "available_fields[3]: name,phone,email"


def test_empty_array():
    assert to_toon({"results": []}) == "results[0]:"


def test_tabular_array_declares_columns_once():
    data = {"results": [
        {"name": "Deansgate Dental", "rating": 4.8, "phone": "+44 161 A"},
        {"name": "City Smile", "rating": 4.7, "phone": "+44 161 B"},
    ]}
    out = to_toon(data)
    lines = out.splitlines()
    assert lines[0] == "results[2]{name,rating,phone}:"
    assert lines[1] == "  Deansgate Dental,4.8,+44 161 A"
    assert lines[2] == "  City Smile,4.7,+44 161 B"


def test_tabular_union_columns_fill_missing_as_empty():
    # sparse rows (Phase-A null-stripping) -> union of columns, blanks for gaps
    data = {"results": [
        {"name": "A", "phone": "p1"},
        {"name": "B", "email": "b@x.co"},
    ]}
    out = to_toon(data)
    lines = out.splitlines()
    assert lines[0] == "results[2]{name,phone,email}:"
    assert lines[1] == "  A,p1,"         # no email -> empty trailing cell
    assert lines[2] == "  B,,b@x.co"     # no phone -> empty middle cell


def test_numeric_looking_strings_are_quoted_for_fidelity():
    # a phone/zip stored as a string must not be read back as a number
    out = to_toon({"results": [{"zip": "0161", "count": 200}]})
    row = out.splitlines()[1]
    assert row == '  "0161",200'         # string zip quoted, real int bare


def test_cell_quoting_for_commas_and_specials():
    data = {"results": [
        {"desc": "Grab all results, phone included", "num_str": "200", "flag": "true"},
    ]}
    out = to_toon(data)
    row = out.splitlines()[1]
    # comma-bearing string quoted; string that looks like a number/bool quoted too
    assert '"Grab all results, phone included"' in row
    assert '"200"' in row
    assert '"true"' in row


def test_number_and_bool_stay_bare():
    out = to_toon({"credits_per_row": 0.2, "premium": False})
    assert out == "credits_per_row: 0.2\npremium: false"


def test_nested_object_indents():
    out = to_toon({"run_id": "r1", "stats": {"is_done": True, "pct": "50%"}})
    assert out == "run_id: r1\nstats:\n  is_done: true\n  pct: 50%"


def test_get_results_shape_is_compact_and_tabular():
    data = {
        "total_results": 87, "page": 1, "returned": 2,
        "results": [
            {"name": "Deansgate Dental", "rating": 4.8, "city": "Manchester"},
            {"name": "City Smile Clinic", "rating": 4.7, "city": "Manchester"},
        ],
        "available_fields": ["name", "rating", "city", "phone"],
    }
    out = to_toon(data)
    assert "results[2]{name,rating,city}:" in out
    assert "available_fields[4]: name,rating,city,phone" in out
    # dramatically smaller than the JSON of the same object
    import json
    assert len(out) < len(json.dumps(data))
