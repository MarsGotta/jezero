"""Project detection shared by the connectors and the device client."""

from __future__ import annotations

from pathlib import Path

from jezero.projects import DEFAULT_RULES, ProjectRules


def test_roots_are_case_insensitive_and_skip_generic_names() -> None:
    rules = ProjectRules(git=False)
    assert rules.project_for("/Users/me/Projects/Billing/web") == "billing"
    assert rules.project_for("/home/me/code/api/src") == "api"
    assert rules.project_for("/home/me/projects/worktrees/site-fix") == "site-fix"
    assert rules.project_for("/home/me/Desktop/notes") is None
    assert rules.project_for(r"C:\Users\me\source\repos\Shop\web") == "shop"


def test_configured_paths_win_and_match_any_home() -> None:
    rules = ProjectRules(paths={"~/clients/acme": "acme", "/srv/stacks": "infra"}, git=False)
    assert rules.project_for("/Users/me/clients/acme/web") == "acme"
    assert rules.project_for("/home/me/clients/acme") == "acme"
    assert rules.project_for(r"C:\Users\me\clients\acme\api") == "acme"
    assert rules.project_for("/srv/stacks/buzz") == "infra"
    assert rules.project_for("/home/me/clients/other") is None


def test_custom_roots_and_aliases() -> None:
    rules = ProjectRules(roots=["work"], aliases={"shop-wt": "shop"}, git=False)
    assert rules.project_for("/home/me/work/Shop-WT/x") == "shop"
    assert rules.project_for("/home/me/projects/api") is None


def test_git_repository_and_worktree(tmp_path: Path) -> None:
    main = tmp_path / "somewhere" / "my-app"
    (main / ".git" / "worktrees" / "fix").mkdir(parents=True)
    (main / "pkg").mkdir()
    tree = tmp_path / "elsewhere" / "my-app-fix"
    tree.mkdir(parents=True)
    (tree / ".git").write_text(f"gitdir: {main}/.git/worktrees/fix\n")
    rules = ProjectRules()
    assert rules.project_for(str(main / "pkg")) == "my-app"
    assert rules.project_for(str(tree)) == "my-app"
    assert ProjectRules(git=False).project_for(str(tree)) is None


def test_sidecar_from_the_device_wins(tmp_path: Path) -> None:
    session = tmp_path / "s1.jsonl"
    session.write_text("{}\n")
    (tmp_path / "s1.jsonl.project").write_text("billing\n")
    rules = ProjectRules(aliases={"billing": "payments"})
    assert rules.project_for("/home/me/projects/other", session_file=session) == "payments"
    assert rules.project_for("/home/me/projects/other", session_file=tmp_path / "x") == "other"


def test_default_rules_skip_tool_folders_and_ids() -> None:
    rules = DEFAULT_RULES
    assert rules.project_for("/w/projects/kit/.tool/worktrees/a/b") == "kit"
    assert rules.project_for("/w/.tool/instances/projects/0f1e2d3c-aaaa-bbbb/x") is None
    assert rules.project_for("/w/projects") is None
    assert rules.project_for(None) is None
    assert rules.project_for("/w/.tool/projects/-/memory") is None
    assert rules.with_aliases({"app-wt": "app"}).project_for("/w/projects/app-wt/T-1") == "app"
