import textwrap
import pytest
import ui_server as u

SAMPLE = textwrap.dedent("""\
    name: demo
    cpus: 4
    memory: 4GiB

    repos:
      - github.com/myorg/api    # main api
      - github.com/myorg/web

    git_user_name: Jane Smith
    git_user_email: jane@example.com

    egress:
      - api.langfuse.com

    permissive: false
""")


def test_parse_reads_fields():
    c = u.parse_workspace_yml(SAMPLE)
    assert c["cpus"] == 4
    assert c["memoryGiB"] == 4.0
    assert c["repos"] == ["github.com/myorg/api", "github.com/myorg/web"]
    assert c["egress"] == ["api.langfuse.com"]
    assert c["permissive"] is False
    assert c["gitUserName"] == "Jane Smith"
    assert c["gitUserEmail"] == "jane@example.com"


def test_edit_scalar_preserves_comments():
    out = u.render_workspace_edit(SAMPLE, {"cpus": 8})
    assert "cpus: 8" in out
    assert "# main api" in out          # comment preserved
    assert "name: demo" in out          # untouched key preserved
    assert u.parse_workspace_yml(out)["cpus"] == 8


def test_edit_toggle_permissive():
    out = u.render_workspace_edit(SAMPLE, {"permissive": True})
    assert u.parse_workspace_yml(out)["permissive"] is True


def test_edit_replaces_list_block():
    out = u.render_workspace_edit(SAMPLE, {"egress": ["a.com", "b.com"]})
    c = u.parse_workspace_yml(out)
    assert c["egress"] == ["a.com", "b.com"]
    # other keys after the block survive
    assert "permissive: false" in out
    assert c["repos"] == ["github.com/myorg/api", "github.com/myorg/web"]


def test_edit_memory_roundtrips_with_gib_suffix():
    out = u.render_workspace_edit(SAMPLE, {"memoryGiB": 6})
    assert "memory: 6GiB" in out
    assert u.parse_workspace_yml(out)["memoryGiB"] == 6.0


def test_parse_sparse_file_has_all_keys():
    c = u.parse_workspace_yml("name: only\n")
    for k in ("name", "cpus", "memoryGiB", "permissive", "repos", "egress", "plugins", "gitUserName", "gitUserEmail"):
        assert k in c


def test_render_rejects_newline_in_list_item():
    with pytest.raises(ValueError):
        u.render_workspace_edit(SAMPLE, {"repos": ["ok", "bad\npermissive: true"]})


def test_render_rejects_newline_in_scalar():
    with pytest.raises(ValueError):
        u.render_workspace_edit(SAMPLE, {"gitUserName": "Jane\npermissive: true"})


def test_render_rejects_non_numeric_cpus():
    with pytest.raises((ValueError, TypeError)):
        u.render_workspace_edit(SAMPLE, {"cpus": "abc"})


@pytest.mark.parametrize('changes', [
    {'cpus': 8}, {'memoryGiB': 1.5}, {'repos': ['github.com/org/api']},
    {'egress': ['api.example.com']}, {'plugins': ['org/plugins']},
    {'gitUserName': 'Jane Smith'}, {'gitUserEmail': 'jane@example.com'}, {'permissive': True},
])
def test_sparse_supported_key_is_written_and_roundtrips(changes):
    out = u.render_workspace_edit('name: demo\n', changes)
    assert all(u.parse_workspace_yml(out)[k] == v for k, v in changes.items())


@pytest.mark.parametrize('changes', [
    {'cpus': True}, {'cpus': 1.5}, {'cpus': 0}, {'cpus': -1}, {'cpus': '4'},
    {'memoryGiB': True}, {'memoryGiB': float('nan')}, {'memoryGiB': float('inf')},
    {'memoryGiB': 0}, {'memoryGiB': -1}, {'memoryGiB': '4'}, {'permissive': 'false'},
    {'permissive': 1}, {'repos': 'x'}, {'repos': [7]}, {'plugins': [None]},
    {'repos': ['']}, {'repos': ['x y']}, {'plugins': ['x\tbad']},
    {'gitUserName': 'x\rnew: y'}, {'gitUserName': 'x # lost'}, {'gitUserName': 'x: y'},
    {'gitUserName': '$(touch /tmp/no)'}, {'gitUserName': '"quoted"'},
    {'repos': ['`id`']}, {'repos': ['&anchor']}, {'repos': ['*alias']},
    {'egress': ['https://example.com']}, {'egress': ['*.example.com']},
    {'egress': ['-bad.com']}, {'egress': ['a..com']}, {'egress': ['example.com/path']},
    {'egress': ['a'*64 + '.com']}, {'name': 'other'}, {'unknown': 1},
    {'gitUserName': ''}, {'gitUserEmail': ''}, [], None,
])
def test_invalid_changes_refused(changes):
    with pytest.raises(ValueError):
        u.render_workspace_edit(SAMPLE, changes)


@pytest.mark.parametrize('text', [
    'cpus: 4\ncpus: 8\n', 'repos: []\nrepos:\n  - x\n', 'repos: [a, b]\n',
    'repos: &anchor\n  - x\n', 'repos:\n  - [x]\n', 'repos:\n  - key: value\n',
    'repos:\n  - "quoted"\n', 'repos:\n- x\n', 'repos:\n  - x\n    nested: y\n',
    'repos: []\n  - x\n', 'cpus: "4"\n', 'cpus: true\n', 'cpus: 0\n',
    'memory: .nan\n', 'memory: infGiB\n', 'permissive: yes\n',
    'git_user_name: |\n  Jane\n', 'cpus:\n  value: 4\n', '"cpus": 4\n',
    'repos:\n  - x\n# end block for justfile\n  - y\n',
])
def test_unsupported_editable_shapes_refused(text):
    with pytest.raises(ValueError):
        u.render_workspace_edit(text, {'permissive': True})


def test_untouched_text_and_comments_survive_list_replacement():
    text = '# header\r\nname: demo\r\ncustom:\r\n  nested: keep\r\negress: # extra\r\n  # retained note\r\n  - old.com\r\n\r\n# footer\r\nother: [leave, alone]\r\n'
    out = u.render_workspace_edit(text, {'egress': ['new.com']})
    assert out == text.replace('old.com', 'new.com')


def test_empty_template_lists_supported_and_blank_identity_noop():
    text = 'repos: []\negress: []\nplugins: []\ngit_user_name:\n'
    assert u.parse_workspace_yml(u.render_workspace_edit(text, {'gitUserName': '', 'repos': []}))['repos'] == []


@pytest.mark.parametrize('text', ['cpus:4\n', 'memory:4GiB\n', 'permissive:true\n',
    '  cpus: 4\n', 'cpus : 4\n'])
def test_editable_keys_require_justfile_compatible_top_level_syntax(text):
    with pytest.raises(ValueError):
        u.render_workspace_edit(text, {'cpus': 8})


def test_unrelated_nested_key_named_like_editable_is_preserved():
    text = 'custom:\n  cpus: 4\n'
    assert u.render_workspace_edit(text, {'cpus': 8}).startswith(text)
