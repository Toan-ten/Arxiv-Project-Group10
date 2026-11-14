"""Bounded, non-executing LaTeX source gathering and normalization."""
from __future__ import annotations

import re
from pathlib import Path


class ParseError(ValueError):
    """Input cannot be interpreted safely by this parser."""


def group(text, pos, opening="{", closing="}"):
    while pos < len(text) and text[pos].isspace():
        pos += 1
    if pos >= len(text) or text[pos] != opening:
        raise ParseError(f"Expected {opening!r} near {text[pos:pos + 70]!r}")
    start, depth = pos + 1, 1
    pos += 1
    while pos < len(text):
        if text[pos] == "\\":
            pos += 2
            continue
        if text[pos] == opening:
            depth += 1
        elif text[pos] == closing:
            depth -= 1
            if depth == 0:
                return text[start:pos], pos + 1
        pos += 1
    raise ParseError(f"Unclosed balanced argument near {text[max(0, start-25):start+90]!r}")


def command(text, pos):
    m = re.match(r"\\([a-zA-Z@]+|.)", text[pos:], re.S)
    if not m:
        raise ParseError("Trailing backslash")
    return m[1], pos + len(m[0])


def options(text, pos):
    while pos < len(text) and text[pos].isspace():
        pos += 1
    if pos < len(text) and text[pos] == "[":
        _, pos = group(text, pos, "[", "]")
    return pos



def citation_keys(text):
    """Read literal citation keys with balanced notes; never expand key macros."""
    result, pos = [], 0
    while pos < len(text):
        if text[pos] != "\\":
            pos += 1
            continue
        name, end = command(text, pos)
        if name == "verb":
            start = end + int(text[end:end + 1] == "*")
            delimiter = text[start:start + 1]
            stop = text.find(delimiter, start + 1) if delimiter else -1
            if stop < 0:
                raise ParseError("Unclosed citation-scanning verbatim")
            pos = stop + 1
            continue
        if name == "begin":
            env, after = group(text, end)
            if env in {"verbatim", "verbatim*", "lstlisting"}:
                if env == "lstlisting":
                    cursor = skip_space(text, after)
                    if text[cursor:cursor + 1] == "[":
                        raw_options, cursor = group(text, cursor, "[", "]")
                        caption = listing_options(raw_options).get("caption")
                        if caption:
                            result.extend(citation_keys(caption))
                closing = r"\end{" + env + "}"
                stop = text.find(closing, after)
                if stop < 0:
                    raise ParseError("Unclosed literal environment: " + env)
                pos = stop + len(closing)
                continue
        if re.fullmatch(r"(?:[cC]ite[a-zA-Z]*|[tT]extcite|[pP]arencite|[aA]utocite|onlinecite)", name) or name == "nocite":
            end += int(text[end:end + 1] == "*")
            start = options(text, end)
            start = options(text, start)
            raw, end = group(text, start)
            for part in raw.split(","):
                key = part.strip()
                # Manual \bibitem identifiers may contain literal spaces.
                # Preserve their spelling and require an exact bibliography
                # match downstream; do not expand macros or invent empty keys.
                # Repeated/non-ASCII whitespace is outside this bounded subset
                # because prose normalization must not change key identity.
                literal_key = re.fullmatch(r"[^{}\\\s]+(?: [^{}\\\s]+)*", key)
                if not literal_key or ("*" in key and (name != "nocite" or key != "*")):
                    raise ParseError("Unsupported/empty citation key syntax: " + repr(key))
                result.append(key)
        pos = end
    return result


def strip_comments(text, *, strict_urls=True):
    text = re.sub(r"\\begin\s*\{(verbatim\*?|lstlisting)\}.*?\\end\{\1\}|\\begin\s*\{comment\}.*?\\end\s*\{comment\}",
                  lambda match: match[0] if match[1] else "", text, flags=re.S)
    out, pos = [], 0
    while pos < len(text):
        if text.startswith(r"\verb", pos):
            m = re.match(r"\\verb\*?([^a-zA-Z\s])", text[pos:])
            if m:
                end = text.find(m[1], pos + len(m[0]))
                if end < 0:
                    raise ParseError("Unclosed inline verbatim")
                out.append(text[pos:end + 1])
                pos = end + 1
                continue
        if text[pos] == "\\":
            literal = re.match(r"\\begin\s*\{(verbatim\*?|lstlisting)\}", text[pos:])
            if literal:
                closing = r"\end{" + literal[1] + "}"
                stop = text.find(closing, pos + literal.end())
                if stop < 0:
                    if not strict_urls:
                        out.append(text[pos:])
                        break
                    raise ParseError("Unclosed literal environment: " + literal[1])
                end = stop + len(closing)
                out.append(text[pos:end])
                pos = end
                continue
            if text.startswith((r"\url", r"\path", r"\href"), pos):
                name, after = command(text, pos)
                if name in {"url", "path", "href"}:
                    # URL readers protect their first balanced argument only.
                    # Root probing is lexical and must not validate unused files.
                    try:
                        start = skip_space(text, options(text, after))
                        if text[start:start + 1] == "{":
                            _, end = group(text, start)
                            out.append(text[pos:end])
                            pos = end
                            continue
                    except ParseError:
                        if strict_urls:
                            raise
            out.append(text[pos:pos + 2])
            pos += 2
        elif text[pos] == "%":
            end = text.find("\n", pos)
            if end < 0:
                break
            # TeX consumes the comment's newline; do not join adjacent words.
            out.append(" ")
            pos = end + 1
        else:
            out.append(text[pos])
            pos += 1
    return "".join(out)




def splice_tokens(text, pos, replacement, end):
    r"""Keep a control-word token boundary when removing an argument scope.

    In TeX, the closing brace in textbf{...\quad}Table prevents the letters
    Table becoming part of the preceding command. String substitution must
    retain that boundary without inventing a visible word space.
    """
    tail = text[end:]
    if re.search(r"(?<!\\)(?:\\\\)*\\[A-Za-z@]+$", replacement) and re.match(r"[A-Za-z@]", tail):
        replacement += "{}"
    return text[:pos] + replacement + tail


def mask_literals(text):
    """Same-length lexical view for structural searches outside literal code."""
    return re.sub(r"\\begin\s*\{(verbatim\*?|lstlisting)\}.*?\\end\{\1\}",
                  lambda match: "".join("\n" if c == "\n" else " " for c in match[0]),
                  text, flags=re.S)


def safe_path(root, relative, parent=None):
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise ParseError("Invalid source path")
    if "\\" in relative or Path(relative).is_absolute() or ":" in relative:
        raise ParseError(f"Unsafe source path: {relative!r}")
    base = root if parent is None else parent
    path = base / relative
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ParseError(f"Source escapes version directory: {relative}") from exc
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ParseError("Symlink in source path")
        if part == root:
            break
    return path



def constant_branch(text, pos, name, *, truth=None, known=(), declarations=False, operand_end=None):
    """Select literal TeX true/false only; unknown predicates stay unsupported.

    No macro expansion or filesystem probing occurs while finding the boundary.
    Declarations inside the conditional are deliberately outside this subset.
    """
    _, start = command(text, pos)
    start = start if operand_end is None else operand_end
    cursor, level, split = start, 1, None
    primitives = {"if", "ifx", "ifnum", "ifdim", "ifdefined", "ifcat", "ifodd",
                  "ifcase", "iftrue", "iffalse", "ifmmode", "ifhmode", "ifvmode",
                  "ifinner", "ifcsname", "ifeof", "ifvoid", "ifhbox", "ifvbox"}
    while cursor < len(text):
        if text[cursor] != "\\":
            cursor += 1
            continue
        token, end = command(text, cursor)
        if token in {"newcommand", "renewcommand", "providecommand", "DeclareRobustCommand"} and declarations:
            # Skip a whole definition while locating conditional boundaries.
            # Only the selected branch will be interpreted by expand().
            after = skip_space(text, end)
            after += int(text[after:after + 1] == "*")
            after = skip_space(text, after)
            if text[after:after + 1] == "{":
                _, after = group(text, after)
            else:
                _, after = command(text, after)
            after = options(text, options(text, after))
            _, cursor = group(text, after)
            continue
        if token in {"def", "gdef", "edef", "xdef", "newcommand", "renewcommand",
                     "providecommand", "DeclareRobustCommand", "newif", "let"}:
            raise ParseError("Declarations inside literal conditional require scoped TeX semantics")
        if token == "verb":
            raise ParseError("Verbatim inside literal conditional is unsupported")
        if token in primitives or token in known:
            level += 1
        elif token.startswith("if") and token not in primitives and token != "iff":
            raise ParseError("Unknown conditional boundary: " + token)
        elif token == "else" and level == 1:
            if split is not None:
                raise ParseError("Duplicate else in literal conditional")
            split = (cursor, end)
        elif token == "or" and level == 1:
            raise ParseError("Unexpected or in literal conditional")
        elif token == "fi":
            level -= 1
            if not level:
                true_branch = text[start:split[0] if split else cursor]
                false_branch = text[split[1]:cursor] if split else ""
                if name in {"iftrue", "iffalse"}:
                    truth = name == "iftrue"
                if truth is None:
                    if true_branch.strip() != false_branch.strip():
                        raise ParseError("Compiler-dependent conditional has different branches: " + name)
                    return true_branch, end
                return (true_branch if truth else false_branch), end
        cursor = end
    raise ParseError("Unclosed literal conditional: " + name)



def supplement_evidence(probe):
    """Recognize explicit document roles, never filenames or incidental prose.

    The probe has comments/literals masked. Balanced groups are skipped, so a
    label inside a macro definition is not a title invocation. This is a bounded
    lexical policy, not TeX execution; uncertain declarations stay ambiguous.
    """
    evidence, pos, body, front = [], 0, False, False
    classes = begins = ends = 0
    phrase = re.compile(r"\b(?:supplemental material|supplementary information|supporting information)\b", re.I)
    try:
        while pos < len(probe):
            pos = skip_space(probe, pos)
            if pos >= len(probe):
                break
            if probe[pos] == "{":
                _, pos = group(probe, pos)
                front = False
                continue
            if probe[pos] != "\\":
                front = False
                pos += 1
                continue
            name, pos = command(probe, pos)
            if name == "let":
                target, pos = command(probe, skip_space(probe, pos))
                pos = skip_space(probe, pos)
                if probe[pos:pos + 1] == "=":
                    pos = skip_space(probe, pos + 1)
                source, pos = command(probe, pos)
                role_commands = {"title", "subtitle", "section", "chapter", "Large", "LARGE", "huge", "Huge", "textbf", "documentclass", "begin", "end", "let"}
                if target in role_commands or source in {"csname", "expandafter"}:
                    return []
                continue
            if name.startswith("if") or name in {"else", "fi", "def", "gdef", "edef", "xdef", "catcode"}:
                return []
            if name in {"newcommand", "renewcommand", "providecommand", "DeclareRobustCommand"}:
                pos = skip_space(probe, pos)
                if probe[pos:pos + 1] == "*":
                    pos = skip_space(probe, pos + 1)
                if probe[pos:pos + 1] == "{":
                    target, pos = group(probe, pos)
                    target = target.strip().lstrip("\\")
                else:
                    target, pos = command(probe, pos)
                if target in {"title", "subtitle", "section", "chapter", "Large", "LARGE", "huge", "Huge", "textbf"}:
                    return []
                pos = options(probe, options(probe, pos))
                _, pos = group(probe, pos)
                continue
            if name == "documentclass":
                classes += 1
                pos = skip_space(probe, pos)
                opts = ""
                if probe[pos:pos + 1] == "[":
                    opts, pos = group(probe, pos, "[", "]")
                _, pos = group(probe, pos)
                if any(re.fullmatch(r"manuscript\s*=\s*suppinfo", v.strip()) for v in opts.split(",")):
                    evidence.append(dict(kind="class_option", value="manuscript=suppinfo"))
            elif name in {"begin", "end"}:
                env, pos = group(probe, pos)
                if env == "document":
                    if name == "begin":
                        begins += 1
                        body = front = True
                    else:
                        ends += 1
                        body = front = False
                        # Anything after the document is not its title.
                        if probe[pos:].strip():
                            break
            elif name in {"title", "subtitle", "section", "chapter"}:
                pos = skip_space(probe, pos)
                if probe[pos:pos + 1] == "*":
                    pos += 1
                pos = options(probe, pos)
                value, pos = group(probe, pos)
                label = normalize(value)
                is_heading = name in {"section", "chapter"}
                eligible = (body and front) if is_heading else (not body or front)
                if eligible and phrase.search(label) and (not is_heading or re.fullmatch(phrase.pattern + r"[\s.:]*", label, re.I)):
                    evidence.append(dict(kind=name, value=value))
                if body:
                    front = False
            elif body and front and name in {"Large", "LARGE", "huge", "Huge", "textbf"}:
                start = skip_space(probe, pos)
                if probe[start:start + 1] == "{":
                    value, pos = group(probe, start)
                    if phrase.search(normalize(value)):
                        evidence.append(dict(kind="opening_display_title", value=value))
                    front = False
            elif body and name not in {"centering", "noindent", "raggedright", "raggedleft"}:
                front = False
    except ParseError:
        return []
    return evidence if classes == begins == ends == 1 else []



def auxiliary_document_evidence(probe):
    """Recognize literal independent response/letter/change-note roles only."""
    cls, author, packages, letter = None, None, [], False
    classes = begins = ends = 0
    pos = 0
    try:
        while (pos := skip_space(probe, pos)) < len(probe):
            if probe[pos] == "{":
                _, pos = group(probe, pos)
                continue
            if probe[pos] != "\\":
                pos += 1
                continue
            name, pos = command(probe, pos)
            if name in {"def", "gdef"}:
                target, after = command(probe, skip_space(probe, pos))
                brace = probe.find("{", after)
                if brace < 0:
                    return []
                replacement, pos = group(probe, brace)
                if target in {"documentclass","title","author","usepackage","makelettertitle","begin","end"} or re.search(r"\\(?:documentclass|title|author|usepackage|makelettertitle|begin|end)\b", replacement):
                    return []
                continue
            if name.startswith("if") or name in {"else","fi","let","edef","xdef","catcode"}:
                return []
            if name in {"newcommand","renewcommand","providecommand","DeclareRobustCommand"}:
                pos = skip_space(probe, pos)
                pos += int(probe[pos:pos+1] == "*")
                pos = skip_space(probe, pos)
                if probe[pos:pos+1] == "{":
                    target, pos = group(probe, pos)
                    target = target.strip().lstrip("\\")
                else:
                    target, pos = command(probe, pos)
                if target in {"documentclass","author","usepackage","makelettertitle","begin","end"}:
                    return []
                pos = options(probe, options(probe, pos))
                _, pos = group(probe, pos)
            elif name in {"documentclass","usepackage"}:
                pos = skip_space(probe, pos)
                opts = ""
                if probe[pos:pos+1] == "[":
                    opts, pos = group(probe, pos, "[", "]")
                value, pos = group(probe, pos)
                if name == "documentclass":
                    classes += 1;cls = value
                else:
                    packages.append((value,{v.strip() for v in opts.split(',')}))
            elif name == "author":
                author, pos = group(probe, options(probe, pos))
            elif name in {"begin","end"}:
                env, pos = group(probe, pos)
                if env == "document":
                    if name == "begin":begins += 1
                    else:ends += 1
            elif name == "makelettertitle":
                letter = True
    except ParseError:
        return []
    if classes != 1 or begins != 1 or ends != 1:
        return []
    if any(p == "cvpr" and "rebuttal" in opts for p,opts in packages):
        return [dict(kind="independent_auxiliary_role",value="cvpr_author_response")]
    if cls == "moderncv" and letter:
        return [dict(kind="independent_auxiliary_role",value="cover_letter")]
    if author and re.fullmatch(r"summary of changes(?: over (?:the )?workshop paper)?", normalize(author), re.I):
        return [dict(kind="independent_auxiliary_role",value="summary_of_changes")]
    return []


def listing_options(raw):
    """Balanced listings key/value syntax; values stay source text."""
    result, pos = {}, 0
    while (pos := skip_space(raw, pos)) < len(raw):
        if raw[pos] == ",":
            pos += 1
            continue
        match = re.match(r"[A-Za-z][A-Za-z0-9]*", raw[pos:])
        if not match:
            raise ParseError("Unsupported lstlisting option syntax")
        key = match[0]
        pos = skip_space(raw, pos + len(key))
        value = "true"
        if raw[pos:pos + 1] == "=":
            pos = skip_space(raw, pos + 1)
            if raw[pos:pos + 1] == "{":
                value, pos = group(raw, pos)
            else:
                start = pos
                while pos < len(raw) and raw[pos] != ",":
                    if raw[pos] == "{":
                        _, pos = group(raw, pos)
                    else:
                        pos += 1
                value = raw[start:pos].strip()
        result[key] = value
        pos = skip_space(raw, pos)
        if pos < len(raw) and raw[pos] != ",":
            raise ParseError("Malformed lstlisting option separator")
    return result


def listing_preamble(preamble):
    """Only explicit, unscoped lstset declarations; no style execution."""
    result, pos, depth = {}, 0, 0
    while pos < len(preamble):
        if preamble[pos] == "\\":
            name, end = command(preamble, pos)
            if name == "lstset":
                if depth:
                    raise ParseError("Unsupported scoped lstset configuration")
                raw, end = group(preamble, end)
                result.update(listing_options(raw))
            pos = end
        else:
            depth += int(preamble[pos] == "{") - int(preamble[pos] == "}")
            pos += 1
    return result


class Gatherer:
    """Resolve one documentclass root and only its explicit compilation path."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.visited = []
        self.bib_files = []
        self.bbl_files = []
        self.bbl_evidence = []
        self.bibliography_requests = []
        self.missing_bib_resources = []
        self.root_selection = None
        self.document_root = None
        self.document_class = None
        self.listing_options = {}
        self.local_environments = set()
        self.environment_definitions = {}
        self.encoding = {}
        self.macros = {}
        self.booleans = {}
        self.document_started = False
        self.environment_depth = 0
        self.biblatex_rendering = []
        self.theorems = set()
        self.expanded_bytes = 0
        self.include_only = None
        self.packages = set()
        self.math_aliases_used = set()

    def read(self, path, *, comments=True):
        raw = path.read_bytes()
        if len(raw) > 8 * 1024 * 1024:
            raise ParseError("Source file exceeds 8 MiB")
        try:
            text = raw.decode("utf-8-sig")
            encoding = "utf-8"
        except UnicodeDecodeError:
            # Only an explicit declaration permits a non-UTF8 decoder.
            if not re.search(rb"\\usepackage\[(?:latin1|ansinew)\]\{inputenc\}", raw):
                raise ParseError(f"Unknown source encoding: {path.name}")
            encoding = "cp1252" if b"[ansinew]" in raw else "latin-1"
            text = raw.decode(encoding)
        self.encoding[path.relative_to(self.root).as_posix()] = encoding
        return strip_comments(text) if comments else text

    def resolve(self, name, current, suffix):
        name = name.strip()  # TeX ignores delimiter spaces around a literal filename.
        if re.search(r"[\\#{}$]", name):
            raise ParseError(f"Dynamic include/resource is unsupported: {name}")
        if not Path(name).suffix:
            name += suffix
        candidates = [safe_path(self.root, name, current.parent),
                      safe_path(self.root, name)]
        for path in candidates:
            if path.is_file():
                return path
        raise ParseError(f"Missing compilation resource: {name}")

    def expand(self, path, stack=(), scope_depth=0):
        if path in stack or len(stack) >= 40:
            raise ParseError(f"Include cycle/depth limit: {path.name}")
        text = self.read(path)
        self.expanded_bytes += len(text)
        if self.expanded_bytes > 16 * 1024 * 1024:
            raise ParseError("Expanded source exceeds 16 MiB")
        rel = path.relative_to(self.root).as_posix()
        if rel not in self.visited:
            self.visited.append(rel)
        # Protect definitions: a literal \input in a macro is not an invocation.
        out, pos = [], 0
        while pos < len(text):
            if text[pos] != "\\":
                scope_depth += int(text[pos] == "{") - int(text[pos] == "}")
                out.append(text[pos])
                pos += 1
                continue
            name, end = command(text, pos)
            if name == "verb":
                start = end + int(text[end:end + 1] == "*")
                delimiter = text[start:start + 1]
                stop = text.find(delimiter, start + 1) if delimiter else -1
                if stop < 0:
                    raise ParseError("Unclosed inline verbatim")
                end = stop + 1
                out.append(text[pos:end])
            elif name in {"newcommand", "renewcommand", "providecommand", "DeclareRobustCommand"}:
                start = end
                if text[start:start + 1] == "*":
                    start += 1
                start = skip_space(text, start)
                if text[start:start + 1] == "{":
                    macro, start = group(text, start)
                    macro = macro.lstrip("\\")
                else:
                    macro, start = command(text, start)
                argc, default = 0, None
                start = skip_space(text, start)
                if text[start:start + 1] == "[":
                    count, start = group(text, start, "[", "]")
                    if not count.isdigit():
                        raise ParseError("Invalid macro argument count")
                    argc = int(count)
                    start = skip_space(text, start)
                    if text[start:start + 1] == "[":
                        default, start = group(text, start, "[", "]")
                body, end = group(text, start)
                if name != "providecommand" or macro not in self.macros:
                    self.macros[macro] = (argc, default, body)
                out.append(" ")
            elif name == "begin":
                env, start = group(text, end)
                if env == "document":
                    self.document_started = True
                if env in {"verbatim", "verbatim*", "lstlisting"}:
                    closing = r"\end{" + env + "}"
                    stop = text.find(closing, start)
                    if stop < 0:
                        raise ParseError("Unclosed literal environment: " + env)
                    end = stop + len(closing)
                    out.append(text[pos:end])
                elif env == "thebibliography":
                    # Bibliography style guards belong to the bibliography
                    # reader, not the compilation-path interpreter. No inputs
                    # or conditional bibitems may be hidden by this shortcut.
                    _, start = group(text, start)
                    raw, end = environment(text, start, env)
                    if re.search(r"\\(?:input|include|bibliography|addbibresource)\b", raw):
                        raise ParseError("Dynamic resource inside inline bibliography is unsupported")
                    out.append(text[pos:end])
                else:
                    if env != "document":
                        self.environment_depth += 1
                    out.append(text[pos:end])
            elif name in {"DeclareFieldFormat", "DeclareFieldAlias"} and name not in self.macros:
                if self.document_started or scope_depth:
                    raise ParseError("Scoped/body bibliography format declaration is unsupported")
                start = options(text, end)
                target, start = group(text, start)
                replacement, end = group(text, start)
                self.biblatex_rendering.append(dict(command=name, target=target, raw=replacement))
                out.append(" ")
            elif name in {"newbibmacro", "renewbibmacro", "providebibmacro"} and name not in self.macros:
                if self.document_started or scope_depth:
                    raise ParseError("Scoped/body bibliography rendering declaration is unsupported")
                start = end + int(text[end:end + 1] == "*")
                target, start = group(text, start)
                start = options(text, options(text, start))
                replacement, end = group(text, start)
                self.biblatex_rendering.append(dict(command=name, target=target, raw=replacement))
                out.append(" ")  # already-rendered .bbl data is read separately
            elif name in {"newenvironment", "renewenvironment", "provideenvironment"}:
                start = end + int(text[end:end + 1] == "*")
                env, start = group(text, start)
                argc, default = 0, None
                start = skip_space(text, start)
                if text[start:start + 1] == "[":
                    count, start = group(text, start, "[", "]")
                    if not count.isdigit() or int(count) > 9:
                        raise ParseError("Invalid environment argument count")
                    argc = int(count)
                    start = skip_space(text, start)
                    if text[start:start + 1] == "[":
                        default, start = group(text, start, "[", "]")
                before, start = group(text, start)
                after, end = group(text, start)
                if name == "newenvironment" and env not in self.local_environments:
                    self.environment_definitions[env] = (argc, default, before, after)
                else:
                    self.environment_definitions.pop(env, None)
                self.local_environments.add(env)
                out.append(" ")
            elif (name in self.macros and "\\newtheorem" in self.macros[name][2]
                  and not any(re.search(r"\\" + re.escape(t) + r"\b", self.macros[name][2])
                              for t in ("input", "include", "if", "ifx", "ifnum", "def", "let"))):
                # Expand only a source-written theorem declaration factory.
                # Every command in the replacement must be a declaration.
                argc, default, replacement = self.macros[name]
                commands = set(re.findall(r"\\([A-Za-z@]+)", replacement))
                if argc is None or default is not None or not commands <= {
                    "newtheorem", "newaliascnt", "aliascntresetthe", "crefname", "Crefname"}:
                    raise ParseError("Unsupported theorem declaration factory: " + name)
                start = end
                for i in range(1, argc + 1):
                    value, start = group(text, start)
                    if not re.fullmatch(r"[A-Za-z]+", value):
                        raise ParseError("Nonliteral theorem declaration argument: " + name)
                    replacement = replacement.replace("#" + str(i), value)
                text = text[:pos] + replacement + text[start:]
                continue
            elif name in {"def", "gdef", "edef", "xdef"}:
                # Parameterized TeX primitives are not expanded as LaTeX macros.
                start = skip_space(text, end)
                macro, start = command(text, start)
                brace = text.find("{", start)
                if brace < 0:
                    raise ParseError("Malformed TeX macro definition")
                body, end = group(text, brace)
                if not text[start:brace].strip():
                    self.macros[macro] = (0, None, body)
                else:
                    self.macros[macro] = (None, None, body)
                out.append(" ")
            elif (name in self.macros and self.macros[name][:2] == (0, None)
                  and re.fullmatch(r"\\(?:begin|end)\s*\{(?:equation\*?|eqnarray\*?|align\*?|alignat\*?|gather\*?|multline\*?|displaymath)\}", self.macros[name][2].strip())):
                # Resolve only explicit zero-argument aliases of math delimiters
                # before Tree searches for the matching environment end. No
                # macro bodies, parameters, includes or conditionals execute.
                replacement = self.macros[name][2].strip()
                out.append(replacement)
                self.math_aliases_used.add("Math environment alias from source: " + name + " -> " + replacement)
            elif name == "newif":
                if name in self.macros:
                    raise ParseError("Locally redefined newif requires TeX semantics")
                target, end = command(text, skip_space(text, end))
                if scope_depth or self.environment_depth or not re.fullmatch(r"if[A-Za-z]+", target) or target in self.macros or target in self.booleans:
                    raise ParseError("Scoped/ambiguous newif declaration: " + target)
                if target in {"ifCLASSOPTIONcompsoc", "ifCLASSOPTIONcaptionsoff", "ifCLASSINFOpdf", "ifpdf", "ifPDFTeX"}:
                    raise ParseError("Locally redefined external class/engine condition: " + target)
                self.booleans[target] = False
                out.append(" ")
            elif name in {flag[2:] + state for flag in self.booleans for state in ("true", "false")}:
                flag = "if" + name[:-(4 if name.endswith("true") else 5)]
                if scope_depth or self.environment_depth or name in self.macros:
                    raise ParseError("Scoped/ambiguous newif assignment: " + name)
                self.booleans[flag] = name.endswith("true")
                out.append(" ")
            elif name in self.booleans:
                if name in self.macros:
                    raise ParseError("Locally redefined newif predicate: " + name)
                selected, after = constant_branch(text, pos, name, truth=self.booleans[name], known=self.booleans, declarations=True)
                self.math_aliases_used.add("Source newif selected: " + name + "=" + str(self.booleans[name]).lower())
                text = splice_tokens(text, pos, selected, after)
                continue
            elif name == "if":
                if name in self.macros:
                    raise ParseError("Locally redefined character conditional requires TeX semantics")
                start = skip_space(text, end)
                # Only two literal character tokens. Control sequences may
                # expand during TeX's comparison, so never assume if0 is false.
                chars = text[start:start + 2]
                if not re.fullmatch(r"[A-Za-z0-9{} ]{2}", chars):
                    raise ParseError("Unsupported conditional/dynamic compilation command: if")
                selected, after = constant_branch(text, pos, name, truth=chars[0] == chars[1], known=self.booleans, operand_end=start + 2)
                self.math_aliases_used.add("Literal TeX character comparison from source: " + repr(chars))
                text = splice_tokens(text, pos, selected, after)
                continue
            elif name in {"ifpdf", "ifPDFTeX"}:
                if name in self.macros:
                    raise ParseError("Locally redefined engine predicate requires TeX semantics: " + name)
                selected, after = constant_branch(text, pos, name, known=self.booleans)
                self.math_aliases_used.add("Engine-independent identical conditional branches: " + name)
                text = splice_tokens(text, pos, selected, after)
                continue
            elif name in {"begingroup", "endgroup"}:
                scope_depth += 1 if name == "begingroup" else -1
                out.append(text[pos:end])
            elif name == "let":
                target, _ = command(text, skip_space(text, end))
                if target in self.booleans or target in {flag[2:] + state for flag in self.booleans for state in ("true", "false")}:
                    raise ParseError("Aliased newif state requires TeX semantics: " + target)
                if target in {"ifCLASSOPTIONcompsoc", "ifCLASSOPTIONcaptionsoff", "ifCLASSINFOpdf"}:
                    raise ParseError("Locally redefined IEEE class condition: " + target)
                out.append(text[pos:end])
            elif name == "PassOptionsToClass" and self.document_class == "IEEEtran":
                raise ParseError("External IEEE class option forwarding is unsupported")
            elif name in {"CLASSOPTIONcompsoctrue", "CLASSOPTIONcompsocfalse",
                          "CLASSOPTIONcaptionsofftrue", "CLASSOPTIONcaptionsofffalse"}:
                raise ParseError("Locally changed IEEE class condition requires scoped TeX semantics: " + name)
            elif name in {"ifCLASSOPTIONcompsoc", "ifCLASSOPTIONcaptionsoff", "ifCLASSINFOpdf"}:
                if self.document_class != "IEEEtran" or name in self.macros:
                    raise ParseError("Unverified IEEE class conditional: " + name)
                if any(re.search(r"[^A-Za-z0-9_=.-]", value) for value in self.class_options):
                    raise ParseError("Nonliteral IEEE class options require TeX expansion")
                option = name.removeprefix("ifCLASSOPTION")
                if name == "ifCLASSOPTIONcompsoc" and self.class_options & {"comsoc", "transmag"}:
                    raise ParseError("Interacting IEEE society class options require class execution")
                truth = None if name == "ifCLASSINFOpdf" else option in self.class_options
                selected, after = constant_branch(text, pos, name, truth=truth, known={
                    "ifCLASSOPTIONcompsoc", "ifCLASSOPTIONcaptionsoff", "ifCLASSINFOpdf"})
                self.math_aliases_used.add("IEEE conditional from literal class options/equal branches: " + name)
                text = splice_tokens(text, pos, selected, after)
                continue
            elif name in {"iftrue", "iffalse"}:
                if name in self.macros:
                    raise ParseError("Locally redefined literal conditional: " + name)
                selected, after = constant_branch(text, pos, name)
                self.math_aliases_used.add("Literal conditional selected from source: " + name)
                text = splice_tokens(text, pos, selected, after)
                continue
            elif name in {"input", "include", "subfile"}:
                # subfiles with their own document environments require TeX semantics.
                if name == "subfile":
                    raise ParseError("subfile compilation is not supported")
                start = skip_space(text, end)
                if text[start:start + 1] == "{":
                    target, end = group(text, start)
                else:
                    m = re.match(r"[^\s\\{}]+", text[start:])
                    if not m:
                        raise ParseError("Invalid unbraced input")
                    target, end = m[0], start + len(m[0])
                if name != "include" or self.include_only is None or target in self.include_only:
                    resource = self.resolve(target, path, ".tex")
                    if resource.suffix.lower() == ".bbl":
                        # A rendered bibliography is data, not executable TeX setup.
                        self.register_bbl(resource, "explicit_input", path, target)
                    else:
                        out.append("\n" + self.expand(resource, (*stack, path), scope_depth) + "\n")
            elif name == "includeonly":
                targets, end = group(text, end)
                self.include_only = {s.strip() for s in targets.split(",")}
            elif name in {"if", "ifx", "ifnum", "ifdim", "ifdefined", "ifcat", "ifodd", "ifcase",
                          "iftrue", "iffalse", "ifmmode", "ifhmode", "ifvmode", "ifinner", "ifcsname",
                          "ifpdf", "ifluatex", "ifxetex", "IfFileExists", "InputIfFileExists",
                          "else", "fi", "import", "subimport"}:
                raise ParseError(f"Unsupported conditional/dynamic compilation command: {name}")
            elif name in {"bibliography", "addbibresource"}:
                start = options(text, end)
                targets, end = group(text, start)
                for target in targets.split(","):
                    requested = dict(command=name, target=target.strip(),
                                     source_file=path.relative_to(self.root).as_posix())
                    self.bibliography_requests.append(requested)
                    try:
                        bib = self.resolve(target.strip(), path, ".bib")
                    except ParseError as exc:
                        if not str(exc).startswith(("Missing compilation resource:",
                                                   "Source escapes version directory:")):
                            raise
                        # Never read outside vN. The root's local .bbl may supply
                        # the rendered list even when the original .bib is absent.
                        self.missing_bib_resources.append(dict(requested, error=str(exc)))
                    else:
                        if bib not in self.bib_files:
                            self.bib_files.append(bib)
                out.append(" ")
            elif name == "end":
                env, after = group(text, end)
                out.append(text[pos:after])
                end = after
                if env == "document":
                    break
                self.environment_depth -= 1
            else:
                out.append(text[pos:end])
            pos = end
        return "".join(out)

    def register_bbl(self, path, association, source, target):
        if path not in self.bbl_files:
            self.bbl_files.append(path)
            self.bbl_evidence.append(dict(
                file=path.relative_to(self.root).as_posix(), association=association,
                source_file=source.relative_to(self.root).as_posix(), target=target))

    def fallback_bbl(self):
        if not self.bibliography_requests:
            return
        if any(name in self.macros for name in ("jobname", "bibliography")):
            raise ParseError("Cannot associate .bbl with locally redefined jobname/bibliography")
        candidates = []
        for raw in (self.document_root.with_suffix(".bbl"),
                    self.root / (self.document_root.stem + ".bbl")):
            path = safe_path(self.root, raw.relative_to(self.root).as_posix())
            if path.is_file() and path not in candidates:
                candidates.append(path)
        if len(candidates) > 1:
            raise ParseError("Ambiguous .bbl files for document root: " + self.document_root.name)
        if candidates:
            self.register_bbl(candidates[0], "document_jobname", self.document_root,
                              self.document_root.stem + ".bbl")

    def gather(self):
        roots, roles = [], {}
        for path in sorted(self.root.rglob("*.tex")):
            # Root detection only needs ASCII syntax; do not decode/parse unused auxiliaries.
            raw_probe = path.read_bytes().decode("latin-1")
            if r"\documentclass" not in raw_probe:
                continue  # no literal root can match, regardless of file name
            probe = mask_literals(strip_comments(raw_probe, strict_urls=False))
            if re.search(r"\\documentclass(?:\s*\[[^\]]*\])?\s*\{", probe):
                roots.append(path)
                roles[path] = supplement_evidence(probe) or auxiliary_document_evidence(probe)
        if len(roots) > 1:
            mains = [path for path in roots if not roles[path]]
            if len(mains) == 1:
                chosen = mains[0]
                self.root_selection = dict(
                    policy="unique_main_with_explicit_supplements",
                    selected=chosen.relative_to(self.root).as_posix(),
                    excluded_supplements=[dict(
                        file=path.relative_to(self.root).as_posix(), evidence=roles[path])
                        for path in roots if path != chosen])
                if any(e["kind"] == "independent_auxiliary_role" for path in roots if path != chosen for e in roles[path]):
                    self.root_selection["policy"] = "unique_main_with_explicit_auxiliary_documents"
                    self.root_selection["excluded_documents"] = self.root_selection.pop("excluded_supplements")
                roots = [chosen]
        if len(roots) != 1:
            raise ParseError(f"Expected exactly one documentclass root, found {len(roots)}")
        self.document_root = roots[0]
        # Establish only literal root class/options before resolving source
        # conditionals. No compiler mode or external package is guessed.
        header = mask_literals(strip_comments(roots[0].read_bytes().decode("latin-1"), strict_urls=False))
        declared = re.search(r"\\documentclass(?:\s*\[([^\]]*)\])?\s*\{([^{}]+)\}", header)
        self.document_class = declared[2].strip() if declared else None
        self.class_options = {v.strip() for v in (declared[1] or "").split(",")} if declared else set()
        text = self.expand(roots[0])
        lexical = mask_literals(text)
        class_match = re.search(r"\\documentclass(?:\s*\[[^\]]*\])?\s*\{([^{}]+)\}", lexical)
        self.document_class = class_match[1].strip() if class_match else None
        self.packages = {name.strip() for packages in re.findall(
            r"\\usepackage(?:\s*\[[^\]]*\])?\s*\{([^{}]+)\}", lexical)
            for name in packages.split(",")}
        self.theorems = set(re.findall(r"\\newtheorem\*?\s*\{([^}]+)\}", lexical))
        self.local_environments |= set(re.findall(
            r"\\(?:newenvironment|renewenvironment|provideenvironment)\*?\s*\{([^{}]+)\}", lexical))
        starts = list(re.finditer(r"\\begin\s*\{document\}", lexical))
        ends = list(re.finditer(r"\\end\s*\{document\}", lexical))
        if len(starts) != 1 or len(ends) != 1 or starts[0].end() > ends[0].start():
            raise ParseError("Ambiguous/missing document environment")
        if "listings" in self.packages and re.search(r"\\begin\s*\{lstlisting\}", text):
            self.listing_options = listing_preamble(lexical[:starts[0].start()])
        return roots[0].relative_to(self.root).as_posix(), text[starts[0].end():ends[0].start()]


def skip_space(text, pos):
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


def environment(text, pos, name):
    """Return body/end for a begin tag already consumed; nested tags are balanced."""
    depth, cursor = 1, pos
    pattern = re.compile(r"\\(begin|end)\s*\{" + re.escape(name) + r"\}")
    for m in pattern.finditer(text, pos):
        depth += 1 if m[1] == "begin" else -1
        if depth == 0:
            return text[cursor:m.start()], m.end()
    raise ParseError(f"Unclosed environment: {name}")


def normalize(text, aliases=None, *, inline=True):
    """Preserve semantic commands/math; remove a small explicit formatting set."""
    text = re.sub(r"\\label\s*\{[^{}]*\}", "", text)
    text = re.sub(r"\\(?:centering|raggedright|raggedleft|midrule|toprule|bottomrule|noindent|small|footnotesize|normalsize|large|Large|bfseries|itshape|rm|em)\b", "", text)
    text = re.sub(r"\\(?:hspace|vspace)\*?\s*\{[^{}]*\}", " ", text)
    # Iteratively unwrap simple formatting groups, retaining their full content.
    for _ in range(20):
        updated = re.sub(r"\\(?:textbf|textit|emph|textrm|textsf|texttt|underline|mbox)\s*\{([^{}]*)\}", r"\1", text)
        if updated == text:
            break
        text = updated
    if inline:
        text = re.sub(r"\\\((.*?)\\\)", lambda m: "$" + m[1].strip() + "$", text, flags=re.S)
    if aliases:
        def cite(m):
            keys = [aliases.get(k.strip(), k.strip()) for k in m[2].split(",")]
            return m[1] + "{" + ",".join(dict.fromkeys(keys)) + "}"
        text = re.sub(r"(\\(?:[cC]ite[a-zA-Z]*|[tT]extcite|[pP]arencite|[aA]utocite|onlinecite)\*?(?:\s*\[[^\]]*\]){0,2})\s*\{([^{}]*)\}", cite, text)
    text = text.replace("~", " ")
    return re.sub(r"\s+", " ", text).strip()


def sentences(text):
    """Split prose periods outside math/braces; keep abbreviations, URLs and decimals."""
    start = pos = braces = 0
    math = None
    while pos < len(text):
        char = text[pos]
        if char == "\\":
            if text.startswith(r"\verb", pos):
                m = re.match(r"\\verb\*?([^a-zA-Z\s])", text[pos:])
                if m:
                    end = text.find(m[1], pos + len(m[0]))
                    if end < 0:
                        raise ParseError("Unclosed inline verbatim")
                    pos = end + 1
                    continue
            name, pos = command(text, pos)
            if re.fullmatch(r"(?:[cC]ite[a-zA-Z]*|[tT]extcite|[pP]arencite|[aA]utocite|onlinecite)", name) or name == "nocite":
                # Punctuation in prenotes/postnotes is inside the citation,
                # not a sentence boundary (e.g. cite[Ch. 5]{key}). Keep the
                # complete invocation on one leaf, including both notes.
                if text[pos:pos + 1] == "*":
                    pos += 1
                pos = options(text, pos)
                pos = options(text, pos)
                _, pos = group(text, pos)
            continue
        if char == "$":
            math = None if math else "$"
        if not math:
            braces += int(char == "{") - int(char == "}")
            if braces < 0:
                raise ParseError("Unbalanced prose braces")
            if char == "." and braces == 0:
                before = text[start:pos + 1]
                after = text[pos + 1:]
                token = before.split()[-1] if before.split() else ""
                abbreviations = r"(?:\b(?:e\.g|i\.e|et al|Fig|Figs|Eq|Eqs|Sec|Secs|Dr|Mr|Prof|vs|etc|cf|No)|\b[A-Z])\.$"
                protected = (re.search(abbreviations, before, re.I)
                             or (pos and text[pos - 1].isdigit() and after[:1].isdigit())
                             or after.startswith(".") or (pos and text[pos - 1] == "."))
                if not protected and (not after or after[0].isspace()):
                    yield before.strip()
                    start = pos + 1
        pos += 1
    if math or braces:
        raise ParseError(f"Unclosed inline math/prose group near {text[start:start+120]!r}")
    if text[start:].strip():
        yield text[start:].strip()
