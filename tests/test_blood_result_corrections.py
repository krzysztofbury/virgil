"""A blood result can be corrected or deleted from the page, with validation."""

import sqlite3

import pytest

from tests.conftest import csrf_token, user_db_path


@pytest.fixture
def marker():
    """One marker with results on two dates, removed afterwards."""
    conn = sqlite3.connect(user_db_path())
    marker_id = conn.execute(
        "INSERT INTO blood_markers (name, category, unit, ref_low, ref_high) VALUES ('ZZ Ferritin', 'ZZ Iron', 'ng/mL', 30, 300)"
    ).lastrowid
    first = conn.execute(
        "INSERT INTO blood_results (marker_id, date, value, flag) VALUES (?, '2026-01-10', 25, 'L')", (marker_id,)
    ).lastrowid
    second = conn.execute(
        "INSERT INTO blood_results (marker_id, date, value, flag) VALUES (?, '2026-06-10', 80, '')", (marker_id,)
    ).lastrowid
    conn.commit()
    try:
        yield marker_id, first, second
    finally:
        conn.execute("DELETE FROM blood_results WHERE marker_id = ?", (marker_id,))
        conn.execute("DELETE FROM blood_markers WHERE id = ?", (marker_id,))
        conn.commit()
        conn.close()


def _result(result_id):
    conn = sqlite3.connect(user_db_path())
    try:
        return conn.execute(
            "SELECT date, value, value_text, flag FROM blood_results WHERE id = ?", (result_id,)
        ).fetchone()
    finally:
        conn.close()


def _post(client, path, **data):
    data["_csrf_token"] = csrf_token(client, "/bloodwork")
    return client.post(path, data=data, follow_redirects=False)


def test_a_correction_rewrites_the_result_and_recomputes_the_flag(auth_client, marker):
    marker_id, first, _ = marker

    response = _post(
        auth_client,
        f"/bloodwork/result/{first}/edit",
        date="2026-01-12",
        value="45.5",
        value_text="",
        flag="auto",
        category="ZZ Iron",
    )

    assert _result(first) == ("2026-01-12", 45.5, "", "")
    assert response.headers["location"].startswith(f"/bloodwork/ZZ%20Iron?marker={marker_id}&msg=")
    assert response.headers["location"].endswith("#marker-results")


def test_a_lab_flag_still_overrides_the_range(auth_client, marker):
    _, first, _ = marker

    _post(auth_client, f"/bloodwork/result/{first}/edit", date="2026-01-10", value="45", flag="H")

    assert _result(first)[3] == "H"


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"date": "2026-06-10", "value": "50"}, "already has a result on 2026-06-10"),
        ({"date": "not a date", "value": "50"}, "valid bloodwork date"),
        ({"date": "2026-01-10", "value": "nan"}, "as a number"),
        ({"date": "2026-01-10", "value": "inf"}, "as a number"),
        ({"date": "2026-01-10", "value": "50", "flag": "X"}, "valid bloodwork result flag"),
    ],
)
def test_an_invalid_correction_changes_nothing(auth_client, marker, fields, error):
    _, first, _ = marker

    response = _post(auth_client, f"/bloodwork/result/{first}/edit", **{"flag": "auto", **fields})

    assert _result(first) == ("2026-01-10", 25.0, "", "L")
    assert "err=" in response.headers["location"]
    assert error.replace(" ", "+") in response.headers["location"]


def test_a_result_can_be_deleted_and_a_gone_one_is_reported(auth_client, marker):
    _, first, second = marker

    _post(auth_client, f"/bloodwork/result/{first}/delete")
    again = _post(auth_client, f"/bloodwork/result/{first}/delete")

    assert _result(first) is None and _result(second) is not None
    assert "no+longer+exists" in again.headers["location"]


def test_the_marker_view_lists_each_result_for_correction(auth_client, marker):
    marker_id, first, second = marker

    html = auth_client.get(f"/bloodwork/all?marker={marker_id}").text
    block = html[html.index('id="marker-results"') : html.index("</details>", html.index('id="marker-results"'))]

    assert "Correct ZZ Ferritin results (2)" in block
    assert block.index(f"/bloodwork/result/{second}/edit") < block.index(f"/bloodwork/result/{first}/edit")
    assert f'action="/bloodwork/result/{first}/delete"' in block
    # 25 against a 30 to 300 range is the range's own L, so it is offered as such
    # and a corrected value gets its flag recomputed.
    first_form = block[block.index(f"/bloodwork/result/{first}/edit") :]
    assert '<option value="auto" selected>' in first_form[: first_form.index("</select>")]


def test_a_marker_name_cannot_break_out_of_the_delete_confirmation(auth_client, marker):
    marker_id, first, _ = marker
    conn = sqlite3.connect(user_db_path())
    conn.execute("UPDATE blood_markers SET name = ? WHERE id = ?", ("ZZ'); alert(1); ('", marker_id))
    conn.commit()
    conn.close()

    html = auth_client.get(f"/bloodwork/all?marker={marker_id}").text
    form = html[html.index(f'action="/bloodwork/result/{first}/delete"') :]
    form = form[: form.index(">")]

    assert 'onsubmit="return confirm(this.dataset.confirm)"' in form
    assert 'data-confirm="Delete the ZZ&#39;); alert(1); (&#39; result' in form


def test_a_text_result_is_not_flagged_from_its_stand_in_number(auth_client, marker):
    _, first, _ = marker

    _post(
        auth_client, f"/bloodwork/result/{first}/edit", date="2026-01-10", value="0", value_text="negative", flag="auto"
    )

    assert _result(first) == ("2026-01-10", 0.0, "negative", "")


def test_editing_a_result_that_is_gone_is_reported(auth_client, marker):
    response = _post(auth_client, "/bloodwork/result/999999/edit", date="2026-01-10", value="5", flag="auto")

    assert "no+longer+exists" in response.headers["location"]


def test_a_marker_outside_the_category_tab_still_lists_its_results(auth_client, marker):
    marker_id, *_ = marker

    html = auth_client.get(f"/bloodwork/SomeOtherTab?marker={marker_id}").text

    assert "Correct ZZ Ferritin results (2)" in html


def test_adding_a_result_refuses_infinity(auth_client, marker):
    marker_id, *_ = marker

    response = _post(auth_client, "/bloodwork/result", marker_id=marker_id, date="2026-03-01", value="inf")

    assert "err=" in response.headers["location"]
    conn = sqlite3.connect(user_db_path())
    try:
        assert (
            conn.execute(
                "SELECT 1 FROM blood_results WHERE marker_id = ? AND date = '2026-03-01'", (marker_id,)
            ).fetchone()
            is None
        )
    finally:
        conn.close()
