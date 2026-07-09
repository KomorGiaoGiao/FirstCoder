from firstcoder.app.picker import TuiPickerItem, TuiPickerState, render_picker, visible_picker_window


def test_picker_moves_within_bounds() -> None:
    picker = TuiPickerState(
        kind="test",
        title="Select:",
        items=[TuiPickerItem(id="one", label="One"), TuiPickerItem(id="two", label="Two")],
    )

    picker.move(-1)
    assert picker.selected_index == 0

    picker.move(5)
    assert picker.selected_index == 1
    assert picker.selected_item == TuiPickerItem(id="two", label="Two")


def test_visible_picker_window_tracks_selection() -> None:
    items = [TuiPickerItem(id=str(index), label=f"Item {index}") for index in range(25)]

    window_start, visible_items = visible_picker_window(items, selected_index=20, limit=20)

    assert window_start == 1
    assert visible_items[0].id == "1"
    assert visible_items[-1].id == "20"


def test_render_picker_includes_count_label_and_detail() -> None:
    picker = TuiPickerState(
        kind="test",
        title="Select:",
        items=[
            TuiPickerItem(id="one", label="One", detail="first"),
            TuiPickerItem(id="two", label="Two", detail="second"),
        ],
        selected_index=1,
        count_label="things",
    )

    rendered = render_picker(picker, limit=1)

    assert "Showing 2-2 of 2 things" in rendered
    assert "> 2. Two second" in rendered
