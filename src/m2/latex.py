"""Bounded, non-executing LaTeX source gathering and normalization."""
from __future__ import annotations

import re
import hashlib
from pathlib import Path


class ParseError(ValueError):
    """Input cannot be interpreted safely by this parser."""


def group(text, pos, opening="{", closing="}", *, inline_literals=True):
    while pos < len(text) and text[pos].isspace():
        pos += 1
    if pos >= len(text) or text[pos] != opening:
        raise ParseError(f"Expected {opening!r} near {text[pos:pos + 70]!r}")
    start, depth = pos + 1, 1
    pos += 1
    while pos < len(text):
        if text[pos] == "\\":
            literal = re.match(r"\\verb(?![A-Za-z])", text[pos:]) if inline_literals else None
            if literal:
                cursor = pos + literal.end()
                cursor += int(text[cursor:cursor + 1] == "*")
                delimiter = text[cursor:cursor + 1]
                if not delimiter or delimiter.isspace() or delimiter.isalpha() or not 33 <= ord(delimiter) <= 126:
                    raise ParseError("Malformed inline literal in balanced argument")
                stop = text.find(delimiter, cursor + 1)
                if stop < 0 or "\n" in text[cursor + 1:stop] or "\r" in text[cursor + 1:stop]:
                    raise ParseError("Unclosed inline literal in balanced argument")
                pos = stop + 1
                continue
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


_COMMAND_RE = re.compile(r"\\([a-zA-Z@]+|.)", re.S)


def latex_replacement(text, pos):
    """Đọc argument declaration dạng group hoặc một token, không thực thi.

    Replacement của primitive def vẫn bắt buộc dùng group(), không đi qua đây.
    """
    pos = skip_space(text, pos)
    if text[pos:pos+1] == "{":
        return group(text, pos, inline_literals=False)
    if text[pos:pos+1] == "\\":
        token, end = command(text, pos)
        if token.isspace():
            raise ParseError("Unsupported whitespace replacement token")
        return text[pos:end], end
    if pos < len(text) and re.fullmatch(r"[A-Za-z0-9.,;:+*/=-]", text[pos]):
        return text[pos], pos + 1
    raise ParseError("Unsupported/missing LaTeX replacement token")



def algorithm_declaration(text, pos, name):
    """Đọc declaration algorithm2e literal; không thực thi macro hoặc hook."""
    target, pos = group(text, pos)
    label, pos = group(text, pos)
    if (not re.fullmatch(r"[A-Za-z]+", target)
            or target in {"begin", "end", "input", "include", "section", "subsection",
                          "If", "Else", "For", "While", "Return", "caption", "label"}):
        raise ParseError("Unsafe algorithm declaration target")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 _./:-]*", label):
        raise ParseError("Unsupported dynamic algorithm declaration label")
    if name == "SetKwFunction" and not re.fullmatch(r"F[A-Z][A-Za-z]*", target):
        raise ParseError("Unsupported/colliding algorithm function target")
    if name == "SetKwProg" and target not in {"Fn", "Try", "Catch", "Proc", "Procedure", "Function", "Def"}:
        raise ParseError("Unsupported/colliding algorithm program target")
    if name in {"SetKwInput", "SetKwInOut"} and not (
            re.fullmatch(r"Kw[A-Z][A-Za-z]*", target) or target in {"Input", "Output", "Data", "Require", "Ensure"}):
        raise ParseError("Unsupported/colliding algorithm input target")
    result = dict(command=name, target=target, label=label)
    if name == "SetKwProg":
        separator, pos = group(text, pos)
        ending, pos = group(text, pos)
        if separator not in {"", ":"} or not re.fullmatch(r"[A-Za-z ]*", ending):
            raise ParseError("Unsupported algorithm program separator/ending")
        result.update(separator=separator, ending=ending)
    return result, pos



def command(text, pos):
    m = _COMMAND_RE.match(text, pos)
    if not m:
        # A lone trailing backslash has no command name. Treat it as a
        # line break so the source can still be parsed instead of failing
        # on a malformed/stray backslash.
        if pos < len(text) and text[pos] == "\\":
            return "\\", pos + 1
        raise ParseError("Trailing backslash")
    return m.group(1), m.end()


def options(text, pos):
    while pos < len(text) and text[pos].isspace():
        pos += 1
    if pos < len(text) and text[pos] == "[":
        _, pos = group(text, pos, "[", "]")
    return pos




def literal_length_target(text, pos):
    """Kernel setlength's first argument: one literal register token or group."""
    start = skip_space(text,pos)
    if text[start:start+1] == "{":
        return group(text,start)
    if text[start:start+1] == "\\":
        name,end = command(text,start)
        if re.fullmatch(r"[A-Za-z]+",name):
            return "\\"+name,end
    raise ParseError("Expected literal layout register")

def grouped_natbib_keys(text):
    """Remove only redundant groups around individual literal natbib keys."""
    probe = mask_root_inline_verbatim(mask_literals(text));edits=[];cursor=0
    while cursor < len(probe):
        if probe[cursor] != "\\":
            cursor += 1;continue
        name,end = command(probe,cursor);cursor=end
        if not re.fullmatch(r"(?:[cC]ite(?:p|t|alp|alt|author|year|yearpar|num|s)?|[tT]extcite|[pP]arencite|[aA]utocite|onlinecite)",name):
            continue
        end += int(text[end:end+1]=="*")
        start=options(text,options(text,end))
        raw,stop=group(text,start);keys=[];changed=False
        for token in raw.split(","):
            key=token.strip()
            if key.startswith("{"):
                inner,tail=group(key,0)
                if tail!=len(key) or not re.fullmatch(r"[^{}\s\\,*]+(?: [^{}\s\\,*]+)*",inner):
                    raise ParseError("Unsupported grouped citation key: "+repr(key))
                key=inner;changed=True
            keys.append(key)
        if changed:edits.append((start,stop,"{"+",".join(keys)+"}"))
        cursor=stop
    for start,stop,value in reversed(edits):text=text[:start]+value+text[stop:]
    return text


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



def constant_branch(text, pos, name, *, truth=None, known=(), declarations=False, operand_end=None, projection=None):
    """Select literal TeX true/false only; unknown predicates stay unsupported.

    No macro expansion or filesystem probing occurs while finding the boundary.
    Active declarations need the explicitly supported declaration subset.
    Inactive definitions are token-scanned without registering or executing them.
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
        decision = (name == "iftrue") if name in {"iftrue", "iffalse"} else truth
        inactive = (decision is False and split is None or decision is True and split is not None)
        if token in {"newcommand", "renewcommand", "providecommand", "DeclareRobustCommand"} and declarations and not inactive:
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
            # Inactive TeX branches scan tokens but never execute definitions.
            # Do not skip replacement bodies: nested conditional tokens still
            # participate in TeX's skipped-branch balancing.
            if inactive:
                cursor = end
                continue
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
                        if projection is None or projection(true_branch) != projection(false_branch):
                            raise ParseError("Compiler-dependent conditional has different branches: " + name)
                        return projection(true_branch), end
                    return true_branch, end
                return (true_branch if truth else false_branch), end
        cursor = end
    raise ParseError("Unclosed literal conditional: " + name)



def preamble_rendering_projection(text):
    """Canonical M2 evidence for rendering-only preamble branches, not truth."""
    packages, pos = [],0
    while (pos := skip_space(text,pos)) < len(text):
        name,pos = command(text,pos)
        if name == "DeclareGraphicsExtensions":
            value,pos = group(text,pos)
            if not re.fullmatch(r"\s*\.[A-Za-z0-9]+(?:\s*,\s*\.[A-Za-z0-9]+)*\s*",value):
                raise ParseError("Unsupported graphics-extension conditional")
        elif name == "usepackage":
            raw_options = "";cursor = skip_space(text,pos)
            if text[cursor:cursor+1] == "[":
                raw_options,pos = group(text,cursor,"[","]")
            value,pos = group(text,pos);names = [v.strip() for v in value.split(',')]
            if not set(names) <= {"graphicx","hyperref","color","xcolor","epstopdf"}:
                raise ParseError("Unsupported conditional preamble package")
            for item in raw_options.split(','):
                item=item.strip()
                if not item:continue
                if not re.fullmatch(r"(?:dvipdfmx|pdftex|dvips|final|hyperindex|colorlinks(?:=(?:true|false))?|backref=page|(?:linkcolor|filecolor|citecolor|urlcolor|allcolors)=[A-Za-z0-9!.-]+)",item):
                    raise ParseError("Unsupported conditional package interpretation option")
            packages.extend(n for n in names if n != "epstopdf")
        elif name == "hypersetup":
            _, pos = group(text, pos)
        else:
            raise ParseError("Unsupported conditional preamble rendering command: " + name)
    return ''.join(r'\usepackage{'+name+'}' for name in packages)


def supplement_evidence(probe, *, title_pattern=None):
    """Recognize explicit document roles, never filenames or incidental prose.

    The probe has comments/literals masked. Balanced groups are skipped, so a
    label inside a macro definition is not a title invocation. This is a bounded
    lexical policy, not TeX execution; uncertain declarations stay ambiguous.
    """
    evidence, pos, body, front = [], 0, False, False
    classes = begins = ends = 0
    phrase = re.compile(title_pattern or r"\b(?:supplemental material|supplemental information|supplementary material|supplementary document|supplementary information|supporting information)\b", re.I)
    def explicit_role(label):
        if title_pattern is not None:
            return phrase.search(label)

        # Prove the role label itself, not the scientific title that follows.
        # Unknown commands before the role can swallow it and remain unsafe.
        pieces, cursor = [], 0
        while cursor < len(label):
            if label[cursor] == "\\":
                name, end = command(label, cursor)
                pieces.append(" " if name == " " else label[cursor:end])
                cursor = end
            else:
                pieces.append(label[cursor]); cursor += 1
        visible = "".join(pieces).replace("{", " ").replace("}", " ")
        boundary = r"(?:^|[:\u2013\u2014]|\s-\s|\\\\)\s*(?:[-(]\s*)?"
        ending = r"(?:[\s.})]*$|\s+(?:for|to|supporting\s+the\s+article)\b|\s*[:\u2013\u2014)]|\s*-\s*$|\s*\\\\)"
        match = re.search(boundary + "(?:" + phrase.pattern + ")" + ending, visible, re.I)
        if not match:
            return None
        cursor = 0
        while cursor < match.end():
            if visible[cursor] == "\\":
                name, cursor = command(visible, cursor)
                if name not in {"textbf", "\\", " ", "protect"}:
                    return None
            else:
                cursor += 1
        return match


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
                role_commands = {"title", "subtitle", "section", "chapter", "Large", "LARGE", "huge", "Huge", "textbf", "protect", "documentclass", "begin", "end", "let"}
                if target in role_commands or source in {"csname", "expandafter"}:
                    return []
                continue
            if name in {"def", "gdef"}:
                target, pos = command(probe, skip_space(probe, pos))
                pos = skip_space(probe, pos)
                if probe[pos:pos + 1] != "{":
                    return []  # Parameterized definitions need TeX semantics.
                value, pos = group(probe, pos, inline_literals=False)
                if target in {"title", "subtitle", "section", "chapter", "Large", "LARGE",
                              "huge", "Huge", "textbf", "protect", "documentclass", "begin", "end"} or "\\" in value:
                    return []
                continue  # Literal metadata, e.g. CTeXPreproc, cannot change roles.
            if name.startswith("if") or name in {"else", "fi", "edef", "xdef", "catcode"}:
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
                if target in {"title", "subtitle", "section", "chapter", "Large", "LARGE", "huge", "Huge", "textbf", "protect"}:
                    return []
                pos = options(probe, options(probe, pos))
                _, pos = latex_replacement(probe, pos)
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
                role_match = explicit_role(label)
                if eligible and role_match and (not is_heading or re.fullmatch(phrase.pattern + r"[\s.:]*", label, re.I)):
                    evidence.append(dict(kind=name, value=value))
                if body:
                    front = False
            elif body and front and name in {"Large", "LARGE", "huge", "Huge", "textbf"}:
                start = skip_space(probe, pos)
                if probe[start:start + 1] == "{":
                    value, pos = group(probe, start)
                    if explicit_role(normalize(value)):
                        evidence.append(dict(kind="opening_display_title", value=value))
                    front = False
            elif body and name not in {"centering", "noindent", "raggedright", "raggedleft"}:
                front = False
    except ParseError:
        return []
    return evidence if classes == begins == ends == 1 else []



def literal_input_filename(text, pos, *, allow_unbraced=False):
    """Read a literal filename; path confinement still belongs to resolve()."""
    start = skip_space(text, pos)
    if text[start:start + 1] == "{":
        target, end = group(text, start)
    elif allow_unbraced:
        match = re.match(r"[^\s\\{}]+", text[start:])
        if not match:
            raise ParseError("Invalid unbraced input")
        target, end = match[0], start + len(match[0])
        if text[end:end + 1] in {"\\", "{"}:
            raise ParseError("Dynamic unbraced input filename is unsupported")
    else:
        target, end = group(text, start)
    if not target.strip() or re.search(r"[\\#{}$^~]", target):
        raise ParseError("Dynamic input filename is unsupported")
    return target, end


def mask_inactive_root_blocks(raw_probe):
    """Mask only self-contained, literal, non-nested iffalse blocks for roots.

    No conditional is executed. Any active command outside the blocks, nested
    or unknown condition, else, malformed boundary, or literal scanner ambiguity
    leaves the ENTIRE probe unchanged. This intentionally favors ambiguity.
    """
    probe = strip_comments(raw_probe, strict_urls=False)
    # strip_comments removes comment environments. In skipped TeX their scanner
    # does not run, so hidden else/fi tokens cannot be certified from that view.
    if re.search(r"\\begin\s*\{comment\}", raw_probe):
        return probe
    spans, pos = [], 0
    while pos < len(probe):
        if probe[pos] in "{}":
            return probe  # Group/macro argument semantics are not root evidence.
        if probe[pos] != "\\":
            pos += 1
            continue
        try:
            name, after = command(probe, pos)
        except ParseError:
            return probe
        if name != "iffalse":
            return probe  # Includes definitions/aliases/engine predicates.
        start, cursor = pos, after
        while cursor < len(probe):
            if probe[cursor] != "\\":
                cursor += 1
                continue
            try:
                token, end = command(probe, cursor)
            except ParseError:
                return probe
            if token == "fi":
                spans.append((start, end))
                pos = end
                break
            if token.startswith("if") or token in {"else", "or", "verb", "url", "path", "href"}:
                return probe
            if token in {"begin", "end"}:
                try:
                    env, _ = group(probe, end)
                except ParseError:
                    return probe
                if env in {"verbatim", "verbatim*", "lstlisting", "comment"}:
                    return probe  # Such scanners do not execute in skipped TeX.
            cursor = end
        else:
            return probe
    if not spans:
        return probe
    masked = list(probe)
    for start, end in spans:
        masked[start:end] = ["\n" if c == "\n" else " " for c in probe[start:end]]
    return "".join(masked)


def mask_root_inline_verbatim(probe):
    """Same-length root-only view; never mask an uncertain inline literal."""
    spans, pos = [], 0
    while pos < len(probe):
        if probe[pos] != "\\":
            pos += 1
            continue
        match = re.match(r"\\verb(?![a-zA-Z])", probe[pos:])
        if match:
            cursor = pos + len(match[0])
            if probe[cursor:cursor + 1] == "*":
                cursor += 1  # A starred form cannot backtrack into '*' as its delimiter.
            delimiter = probe[cursor:cursor + 1]
            if (not delimiter or not 33 <= ord(delimiter) <= 126 or
                    "a" <= delimiter <= "z" or "A" <= delimiter <= "Z"):
                return probe
            start = cursor + 1
            stop = probe.find(delimiter, start)
            if stop < 0 or "\n" in probe[start:stop] or "\r" in probe[start:stop]:
                return probe  # No proof of a valid, single-line literal.
            spans.append((pos, stop + 1))
            pos = stop + 1
        else:
            try:
                _, pos = command(probe, pos)  # Skip escaped backslashes/control words.
            except ParseError:
                return probe
    if not spans:
        return probe
    masked = list(probe)
    for start, end in spans:
        masked[start:end] = [" " for _ in probe[start:end]]
    return "".join(masked)


def mask_uncalled_root_macro_definitions(probe):
    """Mask isolated, balanced declarations, never execute macro replacements.

    Outside invocations (other than harmless literal primitives) or dynamic
    declaration syntax leave the whole probe unchanged. Ordinary groups are
    scanned, not discarded, so they cannot hide an active documentclass.
    """
    latex = {"newcommand", "renewcommand", "providecommand", "DeclareRobustCommand"}
    primitive = {"def", "gdef"}
    reserved = latex | primitive | {
        "documentclass", "begin", "end", "input", "include", "subfile",
        "usepackage", "iffalse", "iftrue", "fi", "else", "relax", "par", "noindent"}
    spans, pos = [], 0
    try:
        while pos < len(probe):
            if probe[pos] != "\\":
                pos += 1
                continue
            start = pos
            name, pos = command(probe, pos)
            if name in {"relax", "par", "noindent"}:
                continue
            if name not in latex | primitive:
                return probe  # Includes calls, aliases, conditionals and csname.
            pos = skip_space(probe, pos)
            if name in latex and probe[pos:pos + 1] == "*":
                pos = skip_space(probe, pos + 1)
            if name in latex and probe[pos:pos + 1] == "{":
                target, pos = group(probe, pos)
                if not re.fullmatch(r"\s*\\[A-Za-z@]+\s*", target):
                    return probe
                target = target.strip()[1:]
            else:
                target, pos = command(probe, pos)
                if not re.fullmatch(r"[A-Za-z@]+", target):
                    return probe
            if target in reserved:
                return probe
            pos = skip_space(probe, pos)
            if name in latex:
                count = 0
                if probe[pos:pos + 1] == "[":
                    value, pos = group(probe, pos, "[", "]")
                    if not re.fullmatch(r"[0-9]", value.strip()):
                        return probe
                    count = int(value)
                    pos = skip_space(probe, pos)
                    if probe[pos:pos + 1] == "[":
                        if count == 0:
                            return probe
                        _, pos = group(probe, pos, "[", "]")
            else:
                brace = probe.find("{", pos)
                if brace < 0:
                    return probe
                parameters = re.sub(r"\s+", "", probe[pos:brace])
                if not re.fullmatch(r"(?:#[1-9])*", parameters):
                    return probe
                if parameters != "".join(f"#{i}" for i in range(1, len(parameters) // 2 + 1)):
                    return probe
                pos = brace
            _, pos = latex_replacement(probe, pos)
            spans.append((start, pos))
    except ParseError:
        return probe
    masked = list(probe)
    for start, end in spans:
        masked[start:end] = ["\n" if c == "\n" else " " for c in probe[start:end]]
    return "".join(masked)


def root_selection_probe(raw_probe):
    """Root evidence only; source expansion always reads the original file."""
    return mask_uncalled_root_macro_definitions(
        mask_root_inline_verbatim(mask_literals(mask_inactive_root_blocks(raw_probe))))


def root_document_inputs(probe, *, packages=None, preamble_only=False, fragment=False, strict=False, preamble_inputs=False):
    """Find only explicit, unconditional document-body input/include edges.

    This is root-selection evidence, not conditional execution. Macro bodies,
    comments and literal text cannot introduce edges. Uncertain control flow
    supplies no evidence; normal expansion remains responsible for validation.
    """
    inputs, pos, body = [], 0, fragment
    try:
        while (pos := skip_space(probe, pos)) < len(probe):
            if probe[pos] in "{[":
                opening = probe[pos]
                _, pos = group(probe, pos, opening, "}" if opening == "{" else "]")
                continue
            if probe[pos] != "\\":
                pos += 1
                continue
            name, pos = command(probe, pos)
            if (name.startswith("if") and name != "iff") or name in {
                    "else", "fi", "let", "catcode", "csname", "expandafter",
                    "includeonly", "endinput", "edef", "xdef"}:
                if packages is not None:
                    packages.clear()
                if strict:
                    raise ParseError("Uncertain control flow in root input graph")
                return []
            if name in {"def", "gdef", "newcommand", "renewcommand",
                        "providecommand", "DeclareRobustCommand"}:
                pos = skip_space(probe, pos)
                if probe[pos:pos + 1] == "*":
                    pos = skip_space(probe, pos + 1)
                if probe[pos:pos + 1] == "{":
                    target, pos = group(probe, pos)
                    target = target.strip().lstrip("\\")
                else:
                    target, pos = command(probe, pos)
                if target in {"input", "include", "subfile", "usepackage", "begin", "end", "documentclass"}:
                    if packages is not None:
                        packages.clear()
                    if strict:
                        raise ParseError("Redefined structural command in root input graph")
                    return []
                if name not in {"def", "gdef"}:
                    pos = options(probe, options(probe, pos))
                _, pos = (group(probe, pos, inline_literals=False) if name in {"def", "gdef"} else latex_replacement(probe, pos))
            elif name in {"usepackage", "RequirePackage"} and not body:
                value, pos = group(probe, options(probe, pos))
                if packages is not None and not re.search(r"[\\#{}]", value):
                    packages.update(v.strip() for v in value.split(","))
            elif name in {"begin", "end"}:
                env, pos = group(probe, pos)
                if env == "document":
                    if name == "end" or preamble_only:
                        break
                    body = True
            elif name == "enddocument":
                break
            elif name in {"input", "include", "subfile"}:
                target, pos = literal_input_filename(probe, pos, allow_unbraced=name == "input")
                if body or preamble_inputs:
                    inputs.append((name, target))
    except ParseError:
        if packages is not None:
            packages.clear()
        if strict:
            raise
        return []
    return inputs


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
                replacement, pos = group(probe, brace, inline_literals=False)
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
                _, pos = latex_replacement(probe, pos)
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


def listing_options(raw, *, ordered=False):
    """Balanced listings key/value syntax; values stay source text."""
    result, pos = ([] if ordered else {}), 0
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
            start = pos
            while pos < len(raw) and raw[pos] != ",":
                if raw[pos] == "{":
                    _, pos = group(raw, pos)
                elif raw[pos] == "\\":
                    _, pos = command(raw, pos)
                elif raw[pos] == "}":
                    raise ParseError("Unbalanced lstlisting option value")
                else:
                    pos += 1
            value = raw[start:pos].strip()
            if value.startswith("{"):
                inner, stop = group(value, 0)
                if stop == len(value):
                    value = inner
        if ordered:
            result.append((key, value))
        else:
            result[key] = value
        pos = skip_space(raw, pos)
        if pos < len(raw) and raw[pos] != ",":
            raise ParseError("Malformed lstlisting option separator")
    return result


def resolve_listing_options(raw, styles, chain=(), *, _budget=None):
    """Apply named styles in source key order; unsafe keys remain for validation."""
    if len(chain) >= 16:
        raise ParseError("lstlisting style depth limit")
    result = {}
    budget = [0] if _budget is None else _budget
    for key, value in listing_options(raw, ordered=True):
        budget[0] += 1
        if budget[0] > 1024:
            raise ParseError("lstlisting style expansion limit")
        if key == "style":
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", value) or value not in styles:
                raise ParseError("Unknown/dynamic lstlisting style: " + value)
            if value in chain:
                raise ParseError("lstlisting style cycle: " + value)
            result.update(resolve_listing_options(styles[value], styles, (*chain, value), _budget=budget))
        else:
            result[key] = value
    return result


def listing_preamble(preamble):
    """Only explicit unscoped declarations; resolve styles without executing hooks."""
    result, styles, languages, pos, depth = {}, {}, set(), 0, 0
    while pos < len(preamble):
        if preamble[pos] == "\\":
            name, end = command(preamble, pos)
            if name in {"lstset", "lstdefinestyle", "lstdefinelanguage"}:
                if depth:
                    raise ParseError("Unsupported scoped listings configuration")
                raw, end = group(preamble, end)
                if name == "lstset":
                    result.update(resolve_listing_options(raw, styles))
                elif name == "lstdefinestyle":
                    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", raw):
                        raise ParseError("Unsupported dynamic listing style name")
                    body, end = group(preamble, end)
                    styles[raw] = body
                else:
                    languages.add(raw)
                    # Its definition is not interpreted. The listing reader
                    # refuses use of this language rather than miss substitutions.
            pos = end
        else:
            depth += int(preamble[pos] == "{") - int(preamble[pos] == "}")
            pos += 1
    result["@styles"], result["@languages"] = styles, languages
    return result


def box_options(raw):
    """Balanced PGF key/value literals; interpretation stays with the box reader."""
    values, pos = [], 0
    while (pos := skip_space(raw, pos)) < len(raw):
        if raw[pos] == ',':
            pos += 1
            continue
        m = re.match(r'[A-Za-z][A-Za-z0-9_ /.:-]*', raw[pos:])
        if not m:
            raise ParseError('Unsupported tcolorbox option syntax')
        key = m[0].strip();pos = skip_space(raw, pos + m.end());value = None
        if raw[pos:pos + 1] == '=':
            pos = skip_space(raw, pos + 1);start = pos
            while pos < len(raw) and raw[pos] != ',':
                if raw[pos] == '{':
                    _, pos = group(raw, pos)
                elif raw[pos] == '\\':
                    _, pos = command(raw, pos)
                elif raw[pos] == '}':
                    raise ParseError('Unbalanced tcolorbox option value')
                else:
                    pos += 1
            value = raw[start:pos].strip()
            if value.startswith('{'):
                inner, stop = group(value, 0)
                if stop == len(value):value = inner
        if pos < len(raw) and raw[pos] != ',':
            raise ParseError('Malformed tcolorbox key separator')
        values.append((key,value))
    return values


def box_styles(preamble):
    """Read only top-level literal style declarations on the compilation path."""
    styles, pos = {}, 0
    while pos < len(preamble):
        if preamble[pos] == '{':
            _, pos = group(preamble, pos)
        elif preamble[pos] == '\\':
            name, pos = command(preamble, pos)
            if name in {'begingroup', 'endgroup'}:
                raise ParseError('Scoped tcolorbox style declarations require TeX semantics')
            if name == 'tcbset':
                raw, pos = group(preamble, pos)
                for key,value in box_options(raw):
                    if re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*/\.style',key) and value is not None:
                        styles[key[:-7]] = value
                    elif '/' in key:
                        raise ParseError('Unsupported tcolorbox global/dynamic style: '+key)
                    else:
                        # Ordinary global defaults use the SAME bounded option
                        # validator as explicit boxes; never discard hooks.
                        styles.setdefault('@global', []).append((key,value))
        else:pos += 1
    return styles


class Gatherer:
    """Resolve one documentclass root and only its explicit compilation path."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.visited = []
        self.bib_files = []
        self.bbl_files = []
        self.bbl_evidence = []
        self.bbl_fallback_reason = None
        self.bibliography_requests = []
        self.missing_bib_resources = []
        self.root_selection = None
        self.document_root = None
        self.document_class = None
        self.listing_options = {}
        self.box_styles = {}
        self.local_environments = set()
        self.environment_definitions = {}
        self.encoding = {}
        self.macros = {}
        self.booleans = {}
        self.condition_symbols = {}
        self.document_started = False
        self.document_ended = False
        self.document_terminator_redefined = False
        self.environment_depth = 0
        self.biblatex_rendering = []
        self.theorems = set()
        # Counters declared by the source. \the<counter> prints a value the parser
        # does not compute, but a declared name is proof the command is intentional.
        self.counters = set()
        self.expanded_bytes = 0
        self.include_only = None
        self.packages = set()
        self.math_aliases_used = set()

    def read(self, path, *, comments=True):
        # Bound the actual read as well as decoding: a large bibliography must
        # not be allocated in full merely to discover that it exceeds the limit.
        with path.open("rb") as stream:
            raw = stream.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ParseError(f"Source file exceeds 8 MiB: {path.name}")
        comments_removed = False
        try:
            text = raw.decode("utf-8-sig")
            encoding = "utf-8"
        except UnicodeDecodeError:
            lexical = mask_literals(strip_comments(raw.decode("latin-1")))
            declared = re.search(r"\\(?:usepackage|RequirePackage)\s*\[\s*(?:latin1|ansinew)\s*\]\s*\{\s*inputenc\s*\}", lexical)
            if declared:
                encoding = "cp1252" if "ansinew" in declared[0] else "latin-1"
                text = raw.decode(encoding)
            elif comments:
                # Latin-1 is only a reversible byte-to-character mapping for
                # lexing comments. Decode ALL remaining bytes strictly as UTF-8;
                # invalid active prose, escaped %, and literal blocks still fail.
                try:
                    active = strip_comments(raw.decode("latin-1")).encode("latin-1")
                    text = active.decode("utf-8-sig")
                    comments_removed = True
                except UnicodeDecodeError as exc:
                    raise ParseError(f"Unknown source encoding: {path.name}") from exc
                encoding = "utf-8 (non-UTF8 bytes confined to comments)"
                self.math_aliases_used.add("Non-UTF8 bytes confined to TeX comments: " +
                                           self.source_label(path))
            else:
                raise ParseError(f"Unknown source encoding: {path.name}")
        self.encoding[self.source_label(path)] = encoding
        return strip_comments(text) if comments and not comments_removed else text

    def resolve(self, name, current, suffix):
        name = name.strip()  # TeX ignores delimiter spaces around a literal filename.
        if re.search(r"[\\#{}$]", name):
            raise ParseError(f"Dynamic include/resource is unsupported: {name}")
        # A dot inside a stem (3.1_intro) is not necessarily an extension.
        # Prefer an exact file at each search location, then try the TeX/Bib
        # suffix without removing or replacing any part of the requested name.
        names = [name] if name.lower().endswith(suffix.lower()) else [name, name + suffix]
        for base in (current.parent, self.root):
            for candidate in names:
                path = safe_path(self.root, candidate, base)
                if path.is_file():
                    return path
        raise ParseError(f"Missing compilation resource: {names[-1]}")

    def expand(self, path, stack=(), scope_depth=0, *, body_mode=None):
        if path in stack or len(stack) >= 40:
            raise ParseError(f"Include cycle/depth limit: {path.name}")
        text = self.read(path)
        if re.search(r"\\(?:let|futurelet)\s*\\enddocument\b", mask_literals(text)):
            # Refuse uncertain alias semantics; do not execute or alter aliases.
            self.document_terminator_redefined = True
        self.expanded_bytes += len(text)
        if self.expanded_bytes > 16 * 1024 * 1024:
            raise ParseError("Expanded source exceeds 16 MiB")
        rel = self.source_label(path)
        if rel not in self.visited:
            self.visited.append(rel)
        if body_mode:
            text = self.included_document_body(path, text, stack, body_mode)
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
                body, end = latex_replacement(text, start)
                if name != "providecommand" or macro not in self.macros:
                    self.macros[macro] = (argc, default, body)
                    self.condition_symbols[macro] = (not scope_depth and not self.environment_depth
                                                     and name != "providecommand")
                out.append(" ")
            elif name in {"usepackage", "RequirePackage"} and not self.document_started and name not in self.macros:
                after_options = options(text, end)
                value, after = group(text, after_options)
                if not re.search(r"[\\#{}]", value):
                    names = {v.strip() for v in value.split(",")}
                    if after_options != skip_space(text, end) and names & {"standalone", "subfiles"}:
                        raise ParseError("Unsupported included-document package options")
                    self.packages.update(names)
                out.append(text[pos:after])
                end = after
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
                before, start = group(text, start, inline_literals=False)
                after, end = group(text, start, inline_literals=False)
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
                if macro == "eqref":
                    brace = text.find("{", start)
                    if brace >= 0:
                        _, end = group(text, brace, inline_literals=False)
                    out.append(" ")
                    pos = end
                    continue
                brace = text.find("{", start)
                if brace < 0:
                    raise ParseError("Malformed TeX macro definition")
                body, end = group(text, brace, inline_literals=False)
                if not text[start:brace].strip():
                    self.macros[macro] = (0, None, body)
                else:
                    self.macros[macro] = (None, None, body)
                self.condition_symbols[macro] = (not scope_depth and not self.environment_depth
                                                  and name in {"def", "gdef"})
                out.append(" ")
            elif (name in self.macros and self.macros[name][:2] == (0, None)
                  and not scope_depth and self.condition_symbols.get(name) is True
                  and re.fullmatch(r"\\(?:begin|end)\s*\{(?:subequations|enumerate|proof)\}(?:\\medskip)?",
                                   self.macros[name][2].strip())):
                # Project an already-defined, unscoped, zero-argument boundary
                # at its actual invocation. Never scan ahead using a later
                # definition, expand arbitrary payload, or drop a spacing tail.
                replacement = self.macros[name][2].strip()
                boundary = re.match(r"\\(begin|end)", replacement)[1]
                self.environment_depth += 1 if boundary == "begin" else -1
                out.append(replacement)
                self.math_aliases_used.add("Literal environment boundary alias: " + name + " -> " + replacement)
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
            elif name == "ifdefined":
                if name in self.macros:
                    raise ParseError("Locally redefined conditional requires TeX semantics: " + name)
                target, cursor = command(text, skip_space(text,end))
                if not self.condition_symbols.get(target):
                    raise ParseError("Unsupported conditional/dynamic compilation command: ifdefined")
                selected, after = constant_branch(text,pos,name,truth=True,known=self.booleans,
                                                  declarations=True,operand_end=cursor)
                self.math_aliases_used.add("ifdefined verified by prior top-level source declaration: " + target)
                text = splice_tokens(text,pos,selected,after)
                continue
            elif name == "ifnum":
                if name in self.macros:
                    raise ParseError("Locally redefined numeric conditional requires TeX semantics")
                def integer_operand(cursor):
                    cursor = skip_space(text,cursor)
                    if text[cursor:cursor+1] == "\\":
                        target,cursor = command(text,cursor)
                        definition = self.macros.get(target)
                        if (not self.condition_symbols.get(target) or not definition
                                or definition[:2] != (0,None) or not re.fullmatch(r"[+-]?[0-9]{1,10}",definition[2].strip())
                                or text[cursor:cursor+1].isdigit()):
                            raise ParseError("Unsupported conditional/dynamic compilation command: ifnum")
                        value = int(definition[2].strip())
                        terminator = "macro_space" if definition[2][-1:].isspace() else "macro"
                    else:
                        match = re.match(r"[+-]?[0-9]{1,10}(?![0-9])",text[cursor:])
                        if not match:
                            raise ParseError("Unsupported conditional/dynamic compilation command: ifnum")
                        cursor += match.end();value = int(match[0])
                        terminator = "literal"
                    if abs(value) > 2147483647:
                        raise ParseError("TeX integer operand out of range")
                    return value,cursor,terminator
                left,cursor,_ = integer_operand(end);cursor = skip_space(text,cursor)
                relation = text[cursor:cursor+1]
                if relation not in {"<", "=", ">"}:
                    raise ParseError("Unsupported conditional/dynamic compilation command: ifnum")
                right,cursor,terminator = integer_operand(cursor+1)
                if terminator == "macro":
                    # Spaces delimiting a control word are not TeX space tokens.
                    # Another digit/macro can therefore extend the same integer.
                    cursor = skip_space(text,cursor)
                    if text[cursor:cursor+1].isdigit():
                        raise ParseError("Unterminated expandable ifnum operand")
                if terminator != "macro_space" and text[cursor:cursor+1] == "\\":
                    token,after = command(text,cursor)
                    if token != "relax" or token in self.macros:
                        raise ParseError("Unterminated expandable ifnum operand")
                    cursor = after  # explicit nonexpandable number terminator
                truth = {"<":left<right,"=":left==right,">":left>right}[relation]
                selected,after = constant_branch(text,pos,name,truth=truth,known=self.booleans,operand_end=cursor)
                self.math_aliases_used.add("ifnum verified literal/source integers: " + str(left)+relation+str(right))
                text = splice_tokens(text,pos,selected,after)
                continue
            elif name == "ifx" and not self.document_started:
                if name in self.macros:
                    raise ParseError("Locally redefined identity conditional requires TeX semantics")
                if self.macros.keys() & {"usepackage", "DeclareGraphicsExtensions"}:
                    raise ParseError("Locally redefined preamble rendering command")
                _,cursor = command(text,skip_space(text,end))
                _,cursor = command(text,skip_space(text,cursor))
                selected,after = constant_branch(text,pos,name,known=self.booleans,operand_end=cursor,
                                                 projection=preamble_rendering_projection)
                self.math_aliases_used.add("Engine-independent equivalent preamble package branches: ifx")
                text = splice_tokens(text,pos,selected,after)
                continue
            elif name in {"ifpdf", "ifPDFTeX"}:
                if name in self.macros:
                    raise ParseError("Locally redefined engine predicate requires TeX semantics: " + name)
                if not self.document_started and self.macros.keys() & {"usepackage", "DeclareGraphicsExtensions"}:
                    raise ParseError("Locally redefined preamble rendering command")
                selected, after = constant_branch(text, pos, name, known=self.booleans,
                                                  projection=None if self.document_started else preamble_rendering_projection)
                self.math_aliases_used.add("Engine-independent identical/projected preamble branches: " + name)
                text = splice_tokens(text, pos, selected, after)
                continue
            elif name in {"begingroup", "endgroup"}:
                scope_depth += 1 if name == "begingroup" else -1
                out.append(text[pos:end])
            elif name == "let":
                target, _ = command(text, skip_space(text, end))
                self.condition_symbols[target] = False
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
                if name == "ifCLASSINFOpdf" and self.macros.keys() & {"usepackage", "DeclareGraphicsExtensions"}:
                    raise ParseError("Locally redefined preamble rendering command")
                truth = None if name == "ifCLASSINFOpdf" else option in self.class_options
                selected, after = constant_branch(text, pos, name, truth=truth, known={
                    "ifCLASSOPTIONcompsoc", "ifCLASSOPTIONcaptionsoff", "ifCLASSINFOpdf"},
                    projection=preamble_rendering_projection if name == "ifCLASSINFOpdf" and not self.document_started else None)
                self.math_aliases_used.add("IEEE conditional from literal class options/equal branches: " + name)
                text = splice_tokens(text, pos, selected, after)
                continue
            elif name in {"iftrue", "iffalse"}:
                if name in self.macros:
                    raise ParseError("Locally redefined literal conditional: " + name)
                selected, after = constant_branch(text, pos, name, known=self.booleans)
                self.math_aliases_used.add("Literal conditional selected from source: " + name)
                text = splice_tokens(text, pos, selected, after)
                continue
            elif name in {"input", "include", "subfile"}:
                if name == "subfile" and name in self.macros:
                    raise ParseError("Locally redefined subfile requires TeX semantics")
                if name == "subfile" and "subfiles" not in self.packages:
                    raise ParseError("subfile requires a literal subfiles package declaration")
                target, end = literal_input_filename(text, end, allow_unbraced=True)
                if name != "include" or self.include_only is None or target in self.include_only:
                    try:
                        resource = self.resolve(target, path, ".tex")
                    except ParseError:
                        # \input{main.bbl} names a bibliography arXiv's bibtex produced,
                        # so the recorded rendering is the file the paper compiled with.
                        resource = None
                        if target.lower().endswith(".bbl"):
                            wanted = Path(target.replace("\\", "/")).name
                            recorded = [item for item in self.recorded_bbl() if item.name == wanted]
                            if len(recorded) == 1:
                                resource = recorded[0]
                        if resource is None:
                            raise
                    if resource.suffix.lower() == ".bbl":
                        # A rendered bibliography is data, not executable TeX setup.
                        self.register_bbl(resource, "explicit_input", path, target)
                    else:
                        mode = "subfile" if name == "subfile" else None
                        if mode is None and "standalone" in self.packages:
                            probe = mask_literals(self.read(resource))
                            declaration = self.root_class(probe)
                            if declaration and declaration[0] == "standalone":
                                mode = "standalone"
                        out.append("\n" + self.expand(resource, (*stack, path), scope_depth,
                                                       body_mode=mode) + "\n")
                        if self.document_ended:
                            break  # An explicit terminator in an input ends TeX globally.
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
            elif name == "enddocument":
                if (name in self.macros or self.document_terminator_redefined or
                        not self.document_started or self.environment_depth or scope_depth):
                    raise ParseError("Ambiguous or redefined document terminator: enddocument")
                out.append(r"\end{document}")
                self.document_ended = True
                self.math_aliases_used.add("Explicit enddocument terminator: " + rel)
                break
            elif name == "end":
                env, after = group(text, end)
                out.append(text[pos:after])
                end = after
                if env == "document":
                    self.document_ended = True
                    if stack:
                        self.math_aliases_used.add("Explicit document termination in included source: " + rel)
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
                file=self.source_label(path), association=association,
                source_file=self.source_label(source), target=target))

    def source_label(self, path):
        """A portable label for evidence that may live beside the version, not inside it."""
        for base in (self.root, self.root.parent):
            try:
                return path.relative_to(base).as_posix()
            except ValueError:
                continue
        return path.name

    def recorded_bbl(self):
        """The bibliography M1 recorded from the compilation arXiv actually ran.

        It lives beside the version rather than inside it, because a version
        directory may only hold the files its manifest lists.
        """
        folder = self.root.parent / "bbl" / self.root.name / "source_bbl"
        if not folder.is_dir():
            return []
        return sorted(path for path in folder.glob("*.bbl") if path.is_file())

    def fallback_bbl(self, cited=False):
        # A source can cite keys without ever issuing \bibliography (the rendered
        # bibliography was uploaded instead), so an explicit request is not the
        # only legitimate reason to look for the root's own .bbl.
        if not self.bibliography_requests and not cited:
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
        if not candidates:
            # Nothing beside the source: use the rendered bibliography M1 recorded,
            # but only when it is unambiguous for this version.
            recorded = self.recorded_bbl()
            matching = [path for path in recorded if path.stem == self.document_root.stem]
            if len(matching) == 1:
                recorded = matching
            if len(recorded) == 1:
                self.register_bbl(recorded[0], "m1_bbl_artifact", self.document_root, recorded[0].name)
            return
        if candidates:
            self.register_bbl(candidates[0], "document_jobname", self.document_root,
                              self.document_root.stem + ".bbl")

    @staticmethod
    def root_class(probe):
        """Read a literal leading class; do not execute definitions/conditions."""
        probe = probe.removeprefix("\ufeff").removeprefix("\xef\xbb\xbf")
        pos = skip_space(probe, 0)
        if not probe.startswith(r"\documentclass", pos):
            return None
        try:
            name, pos = command(probe, pos)
            if name != "documentclass":
                return None
            opts = ""
            pos = skip_space(probe, pos)
            if probe[pos:pos + 1] == "[":
                opts, pos = group(probe, pos, "[", "]")
            cls, pos = group(probe, pos)
            return cls.strip(), opts.strip()
        except ParseError:
            return None

    def included_document_body(self, path, text, stack, mode):
        """Honor explicit subfiles/standalone links, retaining the whole body."""
        probe = mask_literals(text)
        declaration = self.root_class(probe)
        if re.search(r"\\enddocument\b", probe):
            raise ParseError("Early/aliased included document terminator: " + path.name)
        starts = list(re.finditer(r"\\begin\s*\{document\}", probe))
        ends = list(re.finditer(r"\\end\s*\{document\}", probe))
        if mode == "subfile" and declaration is None and not starts and not ends:
            return text  # subfiles also accepts ordinary input fragments.
        if not declaration or len(starts) != 1 or len(ends) != 1 or starts[0].end() > ends[0].start():
            raise ParseError("Ambiguous/missing included document environment: " + path.name)
        cls, options_text = declaration
        if mode == "subfile":
            if cls != "subfiles" or not options_text:
                raise ParseError("Unverified subfile parent declaration: " + path.name)
            parent = self.resolve(options_text, path, ".tex")
            if parent not in stack:
                raise ParseError("Subfile parent is outside the active compilation path: " + path.name)
        elif cls != "standalone":
            raise ParseError("Unverified standalone input: " + path.name)
        if probe[ends[0].end():].strip():
            self.math_aliases_used.add("Post-document source excluded after explicit " +
                                       mode + " terminator: " + path.relative_to(self.root).as_posix())
        self.math_aliases_used.add("Included document body via " + mode + ": " +
                                   path.relative_to(self.root).as_posix())
        return text[starts[0].end():ends[0].start()]

    def root_copy_signature(self, path, probe):
        """Prove a narrow mirror case, not merely matching manuscript titles.

        No input graph or directory-dependent bibliography can be assumed
        equivalent. Only same-jobname byte copies with identical local TeX/Bib
        resources are eligible; other duplicate drafts remain ambiguous.
        """
        if re.search(r"\\(?:input|include|subfile|import|subimport|includeonly|"
                     r"jobname|IfFileExists|InputIfFileExists|openin|read)\b", probe):
            return None
        try:
            for match in re.finditer(r"\\(?:bibliography|addbibresource|usepackage)\b", probe):
                value, _ = group(probe, options(probe, match.end()))
                if re.search(r"[/\\#{}$]", value) or ".." in value:
                    return None
        except ParseError:
            return None
        files, total = {}, 0
        for candidate in path.parent.iterdir():
            if candidate.suffix.lower() not in {".tex", ".bib", ".bbl", ".sty", ".cls", ".cfg"} or not candidate.is_file():
                continue
            safe_path(self.root, candidate.relative_to(self.root).as_posix())
            total += candidate.stat().st_size
            if total > 16 * 1024 * 1024 or len(files) >= 128:
                return None
            files[candidate.name] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        return path.name, tuple(sorted(files.items()))

    def root_preamble_packages(self, parent, probe, root_probes):
        """Packages proven through literal preamble fragments, with all-or-nothing evidence."""
        packages, visited, total = set(), set(), 0

        def walk(path, source, chain):
            nonlocal total
            if path in chain or len(chain) >= 40 or len(visited) >= 128:
                raise ParseError("Root preamble graph cycle/depth/file limit")
            if path in visited:
                return
            visited.add(path)
            edges = root_document_inputs(source, packages=packages, preamble_only=True,
                                         strict=True, preamble_inputs=True)
            for name, target in edges:
                if name != "input":
                    raise ParseError("Unsupported preamble include semantics")
                child = self.resolve(target, path, ".tex").resolve()
                if child in root_probes:
                    raise ParseError("Independent document in preamble graph")
                with child.open("rb") as stream:
                    raw = stream.read(8 * 1024 * 1024 + 1)
                total += len(raw)
                if len(raw) > 8 * 1024 * 1024 or total > 16 * 1024 * 1024:
                    raise ParseError("Root preamble graph size limit")
                source = root_selection_probe(raw.decode("latin-1"))
                if re.search(r"\\(?:documentclass|begin|end)\s*\{(?:document|[^{}]*)\}", source):
                    raise ParseError("Unverified environment in root preamble fragment")
                walk(child, source, (*chain, path))
        try:
            walk(parent, probe, ())
        except (ParseError, OSError):
            return set()
        return packages

    def root_include_relations(self, parent, probe, root_probes):
        """Bounded literal edges through fragments, with no speculative execution.

        Complete independent document candidates are terminal vertices. A cycle,
        uncertain fragment, unsafe path or resource limit invalidates the whole
        traversal; no partially collected evidence can suppress another root.
        """
        relations, visited, byte_count = [], {parent}, 0

        def walk(path, source, chain, fragment):
            nonlocal byte_count
            if len(chain) >= 40:
                raise ParseError("Root input graph depth limit")
            inputs = root_document_inputs(source, fragment=fragment, strict=True)
            for name, target in inputs:
                child = self.resolve(target, path, ".tex").resolve()
                if child in chain:
                    raise ParseError("Root input graph include cycle")
                if len(relations) >= 1024:
                    raise ParseError("Root input graph edge limit")
                relations.append((path, name, target, child, chain))
                if child in root_probes or child.suffix.lower() == ".bbl":
                    continue
                if child in visited:
                    continue
                if len(visited) >= 128:
                    raise ParseError("Root input graph file limit")
                visited.add(child)
                with child.open("rb") as stream:
                    raw = stream.read(8 * 1024 * 1024 + 1)
                byte_count += len(raw)
                if len(raw) > 8 * 1024 * 1024 or byte_count > 16 * 1024 * 1024:
                    raise ParseError("Root input graph source size limit")
                child_probe = root_selection_probe(raw.decode("latin-1"))
                if re.search(r"\\(?:documentclass|enddocument)\b|\\(?:begin|end)\s*\{document\}", child_probe):
                    raise ParseError("Unverified document boundary in root input fragment")
                walk(child, child_probe, (*chain, child), True)

        try:
            walk(parent, probe, (parent,), False)
        except (ParseError, OSError):
            return []
        return relations

    def gather(self):
        roots, roles, probes = [], {}, {}
        for path in sorted(self.root.rglob("*.tex")):
            # Root detection only needs ASCII syntax; do not decode/parse unused auxiliaries.
            raw_probe = path.read_bytes().decode("latin-1")
            if r"\documentclass" not in raw_probe:
                continue  # no literal root can match, regardless of file name
            probe = root_selection_probe(raw_probe)
            if re.search(r"\\documentclass(?:\s*\[[^\]]*\])?\s*\{", probe):
                roots.append(path)
                probes[path] = probe
                roles[path] = supplement_evidence(probe) or auxiliary_document_evidence(probe)
                if not roles[path]:
                    templates = supplement_evidence(probe, title_pattern=r"^(?:Instructions for \*ACL Proceedings|LuaLaTeX and XeLaTeX Template for \*ACL Style Files)$")
                    if templates:
                        roles[path] = [dict(kind="independent_auxiliary_role",value="explicit_template_documentation",source_evidence=templates)]
                declaration = self.root_class(probe)
                if not roles[path] and declaration and declaration[0] == "osa-supplemental-document":
                    roles[path] = [dict(kind="class_role", value="osa-supplemental-document")]
        copies = []
        if len(roots) > 1:
            signatures, unique = {}, []
            # A shallow representative is chosen only AFTER equivalence is
            # established from bytes/resources, never from a preferred filename.
            for path in sorted(roots, key=lambda p: (len(p.relative_to(self.root).parts),
                                                     p.relative_to(self.root).as_posix())):
                signature = self.root_copy_signature(path, probes[path])
                if signature is not None and signature in signatures:
                    copies.append(dict(
                        file=path.relative_to(self.root).as_posix(),
                        equivalent_to=signatures[signature].relative_to(self.root).as_posix(),
                        evidence="identical_jobname_source_and_local_tex_bib_resources"))
                else:
                    unique.append(path)
                    if signature is not None:
                        signatures[signature] = path
            roots = unique
        if len(roots) > 1:
            # A literal declaration in an explicitly included fragment does not
            # make it an independent document. Do not discard unlinked files or
            # independent documents, even when their names suggest a supplement.
            fragments = {path for path in roots if not re.search(
                r"\\(?:begin|end)\s*\{document\}", probes[path])}
            for parent in roots:
                if parent in fragments or roles[parent]:
                    continue
                parent_packages = self.root_preamble_packages(parent, probes[parent], probes)
                for current, name, target, child, chain in self.root_include_relations(
                        parent, probes[parent], probes):
                    kind = "included_document_fragment" if child in fragments else None
                    if child in probes and child not in fragments:
                        declaration = self.root_class(probes[child])
                        if declaration and declaration[0] == "standalone" and "standalone" in parent_packages:
                            kind = "included_document_body"
                        elif declaration and declaration[0] == "subfiles" and name == "subfile" and "subfiles" in parent_packages:
                            try:
                                if self.resolve(declaration[1], child, ".tex") in chain:
                                    kind = "included_document_body"
                            except ParseError:
                                pass
                    if kind:
                        evidence = dict(
                            kind=kind, parent=current.relative_to(self.root).as_posix(),
                            command=name, target=target)
                        if len(chain) > 1:
                            evidence["via"] = [p.relative_to(self.root).as_posix() for p in chain]
                        roles[child] = [evidence]
            mains = [path for path in roots if not roles[path]]
            # A subfiles child names its parent as the class option, but that only
            # counts when the parent provably loads subfiles: an unprovable preamble
            # (conditional, missing include, redefined \usepackage) proves nothing.
            for path in roots:
                if roles[path] or path in fragments:
                    continue
                declaration = self.root_class(probes[path])
                if not declaration or declaration[0] != "subfiles" or not declaration[1]:
                    continue
                try:
                    parent = self.resolve(declaration[1], path, ".tex").resolve()
                    if parent not in roots or parent == path:
                        continue
                    packages = self.root_preamble_packages(parent, probes[parent], probes)
                except ParseError:
                    continue
                if "subfiles" not in packages:
                    continue
                roles[path] = [dict(kind="included_document_body",
                                    parent=parent.relative_to(self.root).as_posix(),
                                    command="documentclass", target=declaration[1])]
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
                if any(e["kind"] in {"included_document_fragment", "included_document_body"} for path in roots
                       if path != chosen for e in roles[path]):
                    self.root_selection["policy"] = "unique_main_with_explicit_included_fragments"
                    if "excluded_supplements" in self.root_selection:
                        self.root_selection["excluded_documents"] = self.root_selection.pop("excluded_supplements")
                roots = [chosen]
        if len(roots) != 1:
            raise ParseError(
                f"Expected exactly one documentclass root, found {len(roots)}: "
                + ", ".join(path.relative_to(self.root).as_posix() for path in sorted(roots)[:6]))
        if copies:
            if self.root_selection is None:
                self.root_selection = dict(policy="equivalent_document_copies",
                                           selected=roots[0].relative_to(self.root).as_posix())
            self.root_selection["equivalent_root_copies"] = copies
        self.document_root = roots[0]
        # Establish only literal root class/options before resolving source
        # conditionals. No compiler mode or external package is guessed.
        header = mask_literals(strip_comments(roots[0].read_bytes().decode("latin-1"), strict_urls=False))
        declared = re.search(r"\\documentclass(?:\s*\[([^\]]*)\])?\s*\{([^{}]+)\}", header)
        self.document_class = declared[2].strip() if declared else None
        self.class_options = {v.strip() for v in (declared[1] or "").split(",")} if declared else set()
        root_document_inputs(header, packages=self.packages, preamble_only=True)
        text = self.expand(roots[0])
        lexical = mask_literals(text)
        cited = bool(re.search(r"\\(?:[cC]ite[a-zA-Z]*|nocite)\s*[\[{]", lexical))
        inline_bibliography = bool(re.search(r"\\begin\s*\{thebibliography\}", lexical))
        # A rendered .bbl is only evidence when nothing else already supplies the
        # bibliography: a parsed .bib or inline bibitems win, as they always did.
        if not self.bbl_files and not self.bib_files and not inline_bibliography and (
                self.bibliography_requests or cited):
            try:
                self.fallback_bbl(cited=cited)
            except ParseError as exc:
                # A sibling .bbl is optional evidence, not a requirement. Keep the
                # version's own failure instead of inventing one from an ambiguity.
                self.bbl_fallback_reason = str(exc)
        class_match = re.search(r"\\documentclass(?:\s*\[[^\]]*\])?\s*\{([^{}]+)\}", lexical)
        self.document_class = class_match[1].strip() if class_match else None
        self.packages = {name.strip() for packages in re.findall(
            r"\\(?:usepackage|RequirePackage)(?:\s*\[[^\]]*\])?\s*\{([^{}]+)\}", lexical)
            for name in packages.split(",")}
        self.theorems = set(re.findall(r"\\newtheorem\*?\s*\{([^}]+)\}", lexical))
        self.counters = {name.strip() for name in re.findall(r"\\newcounter\s*\{([^}]+)\}", lexical)}
        # thm-restate may name a theorem declared through thmtools rather
        # than newtheorem. Only literal preamble declarations with formatting/
        # counter options are evidence; hooks can print content and are refused.
        document_start = re.search(r"\\begin\s*\{document\}", lexical)
        preamble = lexical[:document_start.start()] if document_start else lexical
        if self.packages & {"thmtools", "thm-restate"} and "declaretheorem" not in self.macros:
            scan = 0
            while scan < len(preamble):
                if preamble[scan] == "{":
                    _, scan = group(preamble, scan)
                    continue
                if preamble[scan] == "[":
                    _, scan = group(preamble, scan, "[", "]")
                    continue
                if preamble[scan] != "\\":
                    scan += 1
                    continue
                token, scan = command(preamble, scan)
                if token != "declaretheorem":
                    continue
                cursor = skip_space(preamble, scan)
                settings = None
                if preamble[cursor:cursor + 1] == "[":
                    settings, cursor = group(preamble, cursor, "[", "]")
                theorem, cursor = group(preamble, cursor)
                if not re.fullmatch(r"[A-Za-z]+", theorem):
                    raise ParseError("Unsupported dynamic declaretheorem name")

                for key, value in listing_options(settings or "", ordered=True):
                    if key in {"refname", "Refname"}:
                        if value is None or not re.fullmatch(r"\s*[A-Za-z][A-Za-z -]*\s*,\s*[A-Za-z][A-Za-z -]*\s*", value):
                            raise ParseError("Unsupported declaretheorem option/hook: " + key)
                    elif (key not in {"name", "style", "numberwithin", "sibling", "numbered", "sharenumber", "parent", "numberlike"}
                          or value is None or not re.fullmatch(r"[A-Za-z][A-Za-z0-9 -]*", value)):
                        raise ParseError("Unsupported declaretheorem option/hook: " + key)
                self.theorems.add(theorem)
                scan = cursor
        self.local_environments |= set(re.findall(
            r"\\(?:newenvironment|renewenvironment|provideenvironment)\*?\s*\{([^{}]+)\}", lexical))
        starts = list(re.finditer(r"\\begin\s*\{document\}", lexical))
        ends = list(re.finditer(r"\\end\s*\{document\}", lexical))
        if len(starts) != 1 or len(ends) != 1 or starts[0].end() > ends[0].start():
            raise ParseError("Ambiguous/missing document environment")
        if "listings" in self.packages and re.search(r"\\begin\s*\{lstlisting\}", text):
            self.listing_options = listing_preamble(lexical[:starts[0].start()])
        self.algorithm_initial = []
        self.algorithm_unresolved = []
        self.algorithm2e_alias = False
        if "algorithm2e" in self.packages:
            self.algorithm2e_alias = bool(re.search(
                r"\\(?:usepackage|RequirePackage)\s*\[[^\]]*\balgo2e\b[^\]]*\]\s*\{algorithm2e\}", preamble))
            # Chỉ lấy declaration ở top-level đã qua gathering. Không đưa các
            # định nghĩa trong argument/hook hoặc scope chưa hiểu thành global.
            scoped = bool(re.search(r"\\(?:begingroup|endgroup|bgroup|egroup)\b", preamble))
            cursor = 0
            while not scoped and (cursor := skip_space(preamble, cursor)) < len(preamble):
                if preamble[cursor] == "{":
                    _, cursor = group(preamble, cursor)
                    continue
                if preamble[cursor] != "\\":
                    cursor += 1
                    continue
                token, cursor = command(preamble, cursor)
                if token not in {"SetKwInput", "SetKwInOut", "SetKwFunction", "SetKwProg"} or token in self.macros:
                    continue
                try:
                    declaration, cursor = algorithm_declaration(preamble, cursor, token)
                except ParseError as exc:
                    self.algorithm_unresolved.append(str(exc))
                    self.algorithm_initial.clear()
                    break  # Không sử dụng binding cũ sau declaration chưa hiểu.
                self.algorithm_initial.append(declaration)
        # Defaults-only mdframed is transparent to scientific content.
        # Any setup/style/hook needs a separate option interpreter.
        self.mdframed_default_setup = not (
            re.search(r"\\(?:mdfsetup|mdfdefinestyle|newmdenv)\b", preamble)
            or re.search(r"\\(?:usepackage|RequirePackage)\s*\[[^\]]*\]\s*\{[^}]*\bmdframed\b", preamble))
        self.enumitem_inline = bool(re.search(
            r"\\(?:usepackage|RequirePackage)\s*\[([^\]]*\binline\b[^\]]*)\]\s*\{[^}]*\benumitem\b", preamble)) and not re.search(
                r"\\(?:setlist|newlist|setlistdepth|SetEnumitemKey)\b", preamble)
        if "tcolorbox" in self.packages:
            if "tcbset" in self.macros:
                raise ParseError("Locally redefined tcbset requires TeX semantics")
            self.box_styles = box_styles(preamble)
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
    pieces, cursor = [], 0
    while cursor < len(text):
        if text[cursor] == "\\" and cursor + 1 < len(text):
            if text[cursor + 1].isspace() and not text[cursor + 2:].strip():
                pieces.append(" ")
            else:
                pieces.append(text[cursor:cursor+2])
            cursor += 2
        else:
            pieces.append(text[cursor]); cursor += 1
    text = "".join(pieces).replace("~", " ")
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
