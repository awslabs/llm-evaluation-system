"""Guard against OS command injection in Terraform local-exec provisioners.

Terraform runs a provisioner's ``command`` through ``/bin/sh -c``. Interpolating
a Terraform value into that string (``"... ${var.foo} ..."``) splices it into
shell *source*, so a ``;``, ``&&`` or backtick in the value executes on whatever
host runs ``terraform apply``/``destroy`` — in this repo's deployment path, a
CodeBuild runner holding deploy credentials. That was HackerOne H1-3766442 /
SOCCRE-20501 (CWE-78) against ``infra/platform/eks.tf``.

The fix is to pass values through the provisioner's ``environment`` map instead,
which hands them to the child process as environment variables where shell
metacharacters are inert. This module pins that: it fails if any provisioner's
``command`` regains a ``${...}`` interpolation, and it fails if the three inputs
that reach those provisioners lose their ``validation`` blocks.

Scope note: the rule applies ONLY to ``command`` inside a ``provisioner`` block.
``command = ["/bin/sh", "-c"]`` inside an embedded Kubernetes manifest (see
``infra/platform/kubernetes.tf`` and ``sandbox-security.tf``) is a container
argv that runs in a pod, not a shell string Terraform executes locally, and
those are legitimately templated.

This is deliberately a pytest rather than a CI-only scanner: neither tflint nor
checkov ships a policy for this pattern, and running it in the normal suite means
a developer sees it before pushing. The CI workflow in
``.github/workflows/iac-scan.yml`` adds generic IaC hygiene on top.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

INFRA_DIR = Path(__file__).resolve().parent.parent / "infra"

# Terraform inputs that reach a local-exec provisioner in infra/platform/eks.tf
# and therefore carry a validation block as defense in depth. Keep in sync with
# the environment maps in that file.
SHELL_REACHABLE_VARS = ("project_name", "region", "vpc_id")

_HEREDOC_START = re.compile(r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*<<-?(?P<tag>[A-Za-z_][A-Za-z0-9_]*)\s*$")
_SINGLE_LINE_ASSIGN = re.compile(r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>\".*\")\s*$")
_BLOCK_HEADER = re.compile(r"^\s*(?P<kind>[A-Za-z_][A-Za-z0-9_]*)\b[^=]*\{\s*$")


@dataclass(frozen=True)
class Assignment:
    """One ``name = value`` found in a .tf file, with its enclosing block kinds."""

    file: Path
    line: int
    name: str
    value: str
    block_stack: tuple[str, ...]

    @property
    def in_provisioner(self) -> bool:
        return "provisioner" in self.block_stack


def _strip_noise(line: str) -> str:
    """Remove ``#``/``//`` comments and double-quoted spans so brace counting on
    the remainder is not thrown off by braces that appear inside them."""
    out: list[str] = []
    i = 0
    in_str = False
    while i < len(line):
        ch = line[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch == "#":
            break
        if ch == "/" and line[i + 1 : i + 2] == "/":
            break
        out.append(ch)
        i += 1
    return "".join(out)


def parse_assignments(path: Path) -> list[Assignment]:
    """Extract every ``name = value`` assignment from a .tf file, tracking which
    block kinds enclose it.

    Line-oriented on purpose: HCL heredocs are line-delimited, and a full HCL
    parser is a dependency this repo does not need for one regex-shaped check.
    Quoted spans and comments are stripped before brace counting so a ``{`` in a
    string or comment cannot desynchronise the block stack.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    assignments: list[Assignment] = []
    stack: list[str] = []
    i = 0

    while i < len(lines):
        raw = lines[i]

        heredoc = _HEREDOC_START.search(raw)
        if heredoc:
            tag = heredoc.group("tag")
            body: list[str] = []
            j = i + 1
            while j < len(lines) and lines[j].strip() != tag:
                body.append(lines[j])
                j += 1
            assignments.append(
                Assignment(
                    file=path,
                    line=i + 1,
                    name=heredoc.group("name"),
                    value="\n".join(body),
                    block_stack=tuple(stack),
                )
            )
            # The heredoc body is opaque to block tracking — skip past it and
            # its terminator so braces inside the script are not counted.
            i = j + 1
            continue

        cleaned = _strip_noise(raw)

        single = _SINGLE_LINE_ASSIGN.search(raw.strip())
        if single and "{" not in cleaned:
            assignments.append(
                Assignment(
                    file=path,
                    line=i + 1,
                    name=single.group("name"),
                    value=single.group("value"),
                    block_stack=tuple(stack),
                )
            )

        opens = cleaned.count("{")
        closes = cleaned.count("}")
        if opens > closes:
            header = _BLOCK_HEADER.match(cleaned)
            stack.append(header.group("kind") if header else "?")
        elif closes > opens and stack:
            stack.pop()

        i += 1

    return assignments


def tf_files() -> list[Path]:
    """Our own .tf sources under infra/.

    Skips ``.terraform/`` — that's the gitignored provider/module cache
    ``terraform init`` writes, and the third-party modules vendored into it ship
    example configs that DO interpolate into provisioner commands. Those are
    upstream's code, not ours, and are never applied by this repo (they're
    ``examples/`` directories inside the module archives). Scanning them would
    make this test fail purely as a side effect of having run ``terraform init``
    locally.
    """
    return sorted(
        p
        for p in INFRA_DIR.rglob("*.tf")
        if ".terraform" not in p.parts
    )


def test_infra_tf_files_are_discovered():
    """Sanity check — a parser that silently finds nothing would make every
    other assertion in this module vacuously true."""
    files = tf_files()
    assert len(files) >= 10, f"expected the infra/ .tf files, found {files}"


def test_provisioner_commands_have_no_interpolation():
    """No provisioner ``command`` may interpolate a Terraform value.

    This is the primary CWE-78 regression guard. Pass values via the
    provisioner's ``environment`` map instead; see infra/platform/eks.tf.
    """
    offenders: list[str] = []
    for path in tf_files():
        for assign in parse_assignments(path):
            if assign.name != "command" or not assign.in_provisioner:
                continue
            if "${" in assign.value:
                found = sorted(set(re.findall(r"\$\{[^}]*\}", assign.value)))
                offenders.append(
                    f"{path.relative_to(INFRA_DIR.parent)}:{assign.line} "
                    f"interpolates {found} into a provisioner command"
                )

    assert not offenders, (
        "Terraform interpolation found inside a provisioner command — this is "
        "OS command injection (CWE-78), because Terraform runs the command via "
        "/bin/sh -c on the host doing the apply/destroy.\n\n"
        + "\n".join(f"  - {o}" for o in offenders)
        + "\n\nFix: pass the value through the provisioner's `environment` map "
        "and reference it as a shell variable ($MY_VAR). See "
        "null_resource.wait_for_cluster in infra/platform/eks.tf."
    )


def test_provisioners_found_at_all():
    """The interpolation test above passes trivially if no provisioner commands
    are discovered, so assert we actually see the ones we know exist."""
    commands = [
        a
        for path in tf_files()
        for a in parse_assignments(path)
        if a.name == "command" and a.in_provisioner
    ]
    assert len(commands) >= 3, (
        f"expected at least 3 provisioner commands in infra/, found "
        f"{[(str(c.file.name), c.line) for c in commands]}"
    )


@pytest.mark.parametrize("var_name", SHELL_REACHABLE_VARS)
def test_shell_reachable_variables_are_validated(var_name: str):
    """Values that reach a local-exec provisioner must be constrained.

    Second layer behind the environment map: a value that cannot contain a shell
    metacharacter is not an injection vector even if interpolation is
    reintroduced.
    """
    variables_tf = INFRA_DIR / "platform" / "variables.tf"
    text = variables_tf.read_text(encoding="utf-8")

    block = re.search(
        r'variable\s+"' + re.escape(var_name) + r'"\s*\{(?P<body>.*?)\n\}',
        text,
        re.DOTALL,
    )
    assert block, f'variable "{var_name}" not found in {variables_tf}'
    assert "validation" in block.group("body"), (
        f'variable "{var_name}" reaches a local-exec provisioner in eks.tf but '
        f"has no validation block. Add one constraining it to a character set "
        f"that cannot contain shell metacharacters."
    )


# ---------------------------------------------------------------------------
# Tests for the checker itself. A guard that cannot fail is not a guard, so
# these pin the parser against the exact shape of the reported vulnerability
# and against the shapes it must NOT flag.
# ---------------------------------------------------------------------------

_VULNERABLE = '''
resource "null_resource" "cleanup" {
  triggers = {
    vpc_id = var.vpc_id
  }

  provisioner "local-exec" {
    when    = destroy
    command = <<-EOT
      aws ec2 describe-security-groups \\
        --filters "Name=vpc-id,Values=${self.triggers.vpc_id}"
    EOT
  }
}
'''

_FIXED = '''
resource "null_resource" "cleanup" {
  triggers = {
    vpc_id = var.vpc_id
  }

  provisioner "local-exec" {
    when = destroy
    environment = {
      VPC_ID = self.triggers.vpc_id
    }
    command = <<-EOT
      aws ec2 describe-security-groups \\
        --filters "Name=vpc-id,Values=$VPC_ID"
    EOT
  }
}
'''

# A container argv inside an embedded Kubernetes manifest. Interpolation here is
# fine — it runs in a pod, not on the operator's machine — so the checker must
# not flag it.
_K8S_MANIFEST_COMMAND = '''
resource "kubectl_manifest" "job" {
  yaml_body = <<-YAML
    spec:
      containers:
        - command: ["/bin/sh", "-c"]
  YAML
}

resource "some_resource" "other" {
  exec {
    command = "aws"
    args    = ["eks", "get-token", "--cluster-name", "${local.name}"]
  }
}
'''


def _commands_in(tmp_path: Path, source: str) -> list[Assignment]:
    tf = tmp_path / "sample.tf"
    tf.write_text(source, encoding="utf-8")
    return [a for a in parse_assignments(tf) if a.name == "command" and a.in_provisioner]


def test_checker_flags_the_reported_vulnerability(tmp_path):
    """The pre-fix shape from H1-3766442 must be detected."""
    commands = _commands_in(tmp_path, _VULNERABLE)
    assert len(commands) == 1, f"parser did not find the provisioner command: {commands}"
    assert "${" in commands[0].value


def test_checker_accepts_the_environment_map_fix(tmp_path):
    """The post-fix shape must not be flagged."""
    commands = _commands_in(tmp_path, _FIXED)
    assert len(commands) == 1, f"parser did not find the provisioner command: {commands}"
    assert "${" not in commands[0].value


def test_checker_ignores_non_provisioner_commands(tmp_path):
    """A templated container argv or provider `exec` block is out of scope."""
    tf = tmp_path / "sample.tf"
    tf.write_text(_K8S_MANIFEST_COMMAND, encoding="utf-8")
    assert not [a for a in parse_assignments(tf) if a.name == "command" and a.in_provisioner]
