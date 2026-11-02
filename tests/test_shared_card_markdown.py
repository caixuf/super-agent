"""Readable summaries must not corrupt table cells or fenced source code."""
from agentcore.card_markdown import (
    build_card24_elements,
    build_card_elements,
    card_renders_markdown,
    _md_table_cells,
)


def test_escaped_pipe_is_one_cell():
    body = r'| command | meaning |' + '\n|---|---|\n' + r'| a\|b | **union** |'
    table = build_card_elements(body)[0]
    assert table['rows'] == [{'c0': 'a|b', 'c1': '**union**'}]
    assert _md_table_cells(r'| a\\|b |') == [r'a\\', 'b']
    assert _md_table_cells(r'| x | ends\|') == ['x', 'ends|']


def test_mismatched_fences_do_not_turn_code_into_tables():
    for code in ['````markdown\n```\n| a | b |\n|---|---|\n| 1 | 2 |\n````',
                 '~~~bash\n```\n| a | b |\n|---|---|\n| 1 | 2 |\n~~~']:
        assert build_card_elements(code) == [{'tag': 'markdown', 'content': code}]
        assert card_renders_markdown(code)


def test_crlf_code_and_markdown_are_not_rewritten():
    code = '## Result\r\n\r\n```cpp\r\nauto x = "a|b";\r\n```\r\n'
    assert build_card_elements(code) == [{'tag': 'markdown', 'content': code}]


def test_extra_table_cells_are_not_silently_dropped():
    body = '| a | b |\n|---|---|\n| 1 | 2 | DO NOT LOSE |'
    assert build_card_elements(body) == [{'tag': 'markdown', 'content': body}]
    assert not card_renders_markdown(body)


def test_mismatched_header_keeps_literal_source():
    body = '| a | b | c |\n|---|---|\n| 1 | 2 |'
    assert build_card_elements(body) == [{'tag': 'markdown', 'content': body}]


def test_short_rows_have_empty_cells_not_missing_columns():
    body = '| a | b |\n|---|---|\n| 1 |'
    assert build_card_elements(body)[0]['rows'] == [{'c0': '1', 'c1': ''}]


def test_malformed_row_keeps_whole_table_as_literal_source():
    body = ('before\n| a | b |\n|---|---|\n| 1 | 2 |\n'
            '| 3 | 4 | EXTRA |\n| 5 |\n\nafter')
    assert build_card_elements(body) == [{'tag': 'markdown', 'content': body}]
    assert not card_renders_markdown(body)


def test_malformed_table_does_not_hide_later_valid_table():
    malformed = '| a | b |\n|---|---|\n| 1 | 2 | 3 |'
    valid = '| x | y |\n|---|---|\n| 1 | 2 |'
    body = malformed + '\n\n' + valid
    assert build_card_elements(body) == [
        {'tag': 'markdown', 'content': malformed + '\n\n'},
        {
            'tag': 'table', 'page_size': 10, 'row_height': 'low',
            'freeze_first_column': False,
            'header_style': {'text_size': 'normal', 'background_style': 'grey', 'bold': True},
            'columns': [
                {'name': 'c0', 'display_name': 'x', 'data_type': 'lark_md',
                 'width': 'auto', 'vertical_align': 'top'},
                {'name': 'c1', 'display_name': 'y', 'data_type': 'lark_md',
                 'width': 'auto', 'vertical_align': 'top'},
            ],
            'rows': [{'c0': '1', 'c1': '2'}],
        },
    ]


def test_unclosed_fence_consumes_the_rest_as_code():
    body = '```python\n| a | b |\n|---|---|\n| 1 | 2 |\n\nnot a table either | x |\n'
    assert build_card_elements(body) == [{'tag': 'markdown', 'content': body}]
    assert card_renders_markdown(body)


def test_fence_marker_must_match_type_and_length():
    body = ('~~~~\n```\n| a | b |\n|---|---|\n~~~\n````\n~~~~\n\n'
            '| x | y |\n|---|---|\n| 1 | 2 |\n')
    elements = build_card_elements(body)
    assert [element['tag'] for element in elements] == ['markdown', 'table']
    assert elements[0]['content'].startswith('~~~~\n') and elements[0]['content'].endswith('~~~~\n\n')


def test_lone_unclosed_pipe_row_stays_text():
    body = 'before\n| a | b\n| 1 | 2 |\nafter'
    assert build_card_elements(body) == [{'tag': 'markdown', 'content': body}]


def test_table_rows_require_matching_outer_pipes():
    for body in ('| a | b |\n|---|---|\n| 1 | 2\n',
                 '| a | b |\n|---|---|\n| 1 | 2 | trailing'):
        assert build_card_elements(body) == [{'tag': 'markdown', 'content': body}]
        assert not card_renders_markdown(body)
    # A divider without outer pipes is valid GFM when the cell count still agrees.
    body = '| a | b |\n---|---\n| 1 | 2 |'
    assert build_card_elements(body)[0]['rows'] == [{'c0': '1', 'c1': '2'}]


def test_escaped_pipe_parity_preserves_literal_backslashes():
    body = '| a | b |\n|---|---|\n| one\\|two | c\\\\\\|d |\n'
    table = build_card_elements(body)[0]
    assert table['rows'] == [{'c0': 'one|two', 'c1': 'c\\\\|d'}]
    assert _md_table_cells('| c\\\\|d |') == ['c\\\\', 'd']


def test_more_than_five_valid_tables_falls_back_without_dropping_text():
    tables = ['| a | b |\n|---|---|\n| %d | ok |' % index for index in range(6)]
    body = 'start\n\n' + '\n\n'.join(tables) + '\n\nend'
    elements = build_card_elements(body)
    assert sum(element['tag'] == 'table' for element in elements) == 5
    assert '| 5 | ok |' in elements[-1]['content']
    assert ''.join(element.get('content', '') for element in elements).count('| 5 | ok |') == 1
    assert not card_renders_markdown(body)


def test_build_card24_preserves_unconverted_markdown_blocks_semantically():
    body = '## 标题\n\n**粗体** `代码` [链接](https://example.test)\n\n| a | b |\n|---|---|\n| 1 | 2 |'
    elements = build_card24_elements(body)
    assert [element['tag'] for element in elements] == ['div', 'table']
    text = elements[0]['text']['content']
    assert '标题' in text and '粗体' in text and '代码' in text
    assert 'https://example.test' in text
