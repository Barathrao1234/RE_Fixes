
import os
import re
import html
import json
import javalang
import javalang.tree as jt
import pandas as pd
from pathlib import Path

from method_lineage_generation import LanguageAdapter

# ---------------------------------------------------------------------------
# Module-level compiled patterns (Java 8 safe — no var, no records, etc.)
# ---------------------------------------------------------------------------

re_value_dollar = re.compile(r'@Value\s*\(\s*["\']\$\{([^}]+)\}["\']\s*\)')
re_value_spel_dollar = re.compile(r'@Value\s*\(\s*["\']#\{\s*\$\{([^}]+)\}\s*\}["\']\s*\)')
re_field_decl = re.compile(
    r'(?:private|public|protected)?\s*[\w<>\[\],\s?]+\s+([A-Za-z_]\w*)\s*(?:=|;)', re.M
)
re_configuration_properties = re.compile(
    r'@ConfigurationProperties\s*\(\s*(?:prefix\s*=\s*)?["\']([^"\')]+)["\']\s*\)'
)
re_property_source = re.compile(
    r'@PropertySource\s*\(\s*(?:value\s*=\s*)?["\']([^"\')]+)["\']'
)
re_message_key = re.compile(
    r'messageSource\.getMessage\s*\(\s*["\']([^"\']+)["\']'
)
re_named_query_decl = re.compile(
    r'@NamedQuery\s*\(\s*name\s*=\s*["\']([^"\']+)["\']\s*,\s*query\s*=\s*["\']([\s\S]*?)["\']\s*\)',
    re.MULTILINE,
)
re_any_method_first_string_arg = re.compile(
    r'(?<!@)\b(?:[A-Za-z_]\w*\s*\.\s*)*([A-Za-z_]\w*)\s*\(\s*["\']([^"\']+)["\']',
    re.MULTILINE,
)

# Pre-compiled patterns reused inside fallback_parse / find_calls_in_method
_re_throw_new = re.compile(r'\bthrow\s+new\s+([A-Za-z_]\w+)\s*\(', re.MULTILINE)
_re_unqualified_method_decl = re.compile(
    r'\b(?:public|private|protected)\b[^{;]*\b(\w+)\s*\(',
    re.MULTILINE,
)

# Java 8 method declaration regex — same structure as Java 18 adapter but
# explicitly excludes 'var' as a return type (Java 10+ only).
re_method_decl = re.compile(
    r'''
    ^\s*
    (?:@\w+(?:\([^)]*\))?\s*)*
    (?:(?:public|private|protected)\s+)?
    (?:static\s+|final\s+|synchronized\s+|native\s+|abstract\s+|default\s+)*
    (?:<[^>]+>\s+)?
    (?!var\b)                                        # Java 8: no 'var' type inference
    (?:[A-Za-z_][\w$.]*(?:\s*<[^>{}]+>)?(?:\s*\[\s*\])?\s+)+
    (?!(?:if|for|while|switch|catch|else)\b)
    ([A-Za-z_]\w*)
    \s*\(
    \s*(
        (?:
        (?:@\w+(?:\([^)]*\))?\s*)*
        (?:final\s+)?
        [A-Za-z_][\w$.]*(?:\s*<[^>{}]+>)?(?:\s*\[\s*\])*(?:\s*\.\.\.)?\s+
        [A-Za-z_]\w*
        )
        (?:\s*,\s*
        (?:@\w+(?:\([^)]*\))?\s*)*
        (?:final\s+)?
        [A-Za-z_][\w$.]*(?:\s*<[^>{}]+>)?(?:\s*\[\s*\])*(?:\s*\.\.\.)?\s+
        [A-Za-z_]\w*
        )*
    )?
    \)\s*\{
    ''',
    re.M | re.X
)


# ---------------------------------------------------------------------------
# Module-level helper (mirrors the Java 18 adapter)
# ---------------------------------------------------------------------------

def _strip_source_comments(src: str) -> str:
    """Remove // and /* */ comments while preserving string literals."""
    result = []
    i = 0
    n = len(src)
    in_string = False
    string_char = None

    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""

        if in_string:
            result.append(ch)
            if ch == '\\':
                i += 1
                if i < n:
                    result.append(src[i])
            elif ch == string_char:
                in_string = False
                string_char = None
            i += 1
            continue

        if ch == '/' and nxt == '/':
            while i < n and src[i] != '\n':
                i += 1
            continue

        if ch == '/' and nxt == '*':
            i += 2
            while i < n - 1:
                if src[i] == '*' and src[i + 1] == '/':
                    i += 2
                    break
                i += 1
            continue

        if ch in ('"', "'"):
            in_string = True
            string_char = ch

        result.append(ch)
        i += 1

    return ''.join(result)


# ---------------------------------------------------------------------------
# Java 8 Adapter
# ---------------------------------------------------------------------------

class JavaAdapter(LanguageAdapter):
    """
    LanguageAdapter implementation for Java 8 codebases.

    Compared to the Java 18 adapter:
      - parse_ast uses javalang directly (javalang targets Java 8).
      - No special handling for records, sealed classes, text blocks,
        switch expressions, or 'var' type inference.
      - Lambda bodies and stream chains are captured via the chained-call
        regex path (same approach as the Java 18 adapter's fallback).
      - Default / static interface methods (new in Java 8) are handled
        through get_methods_in_type, which yields MethodDeclaration nodes
        on interface bodies.
    """

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _normalize_ps_ref(self, ps_str: str) -> str:
        if ps_str.startswith("classpath:"):
            return ps_str[len("classpath:"):]
        if ps_str.startswith("file:"):
            return ps_str[len("file:"):]
        return ps_str

    def _get_java_file_indexes(self, java_folder: Path):
        """Build and cache Java file indexes to avoid repeated rglob scans."""
        folder_key = str(Path(java_folder).resolve())
        cache = getattr(self, "_java_file_index_cache", None)
        if isinstance(cache, dict) and cache.get("folder") == folder_key:
            return cache

        all_java_files = list(Path(java_folder).rglob("*.java"))
        stem_to_paths = {}
        rel_no_ext_to_path = {}

        for p in all_java_files:
            rp = p.resolve()
            stem_l = rp.stem.lower()
            stem_to_paths.setdefault(stem_l, []).append(rp)

            try:
                rel_no_ext = str(rp.relative_to(java_folder)).replace("\\", "/")
                if rel_no_ext.lower().endswith(".java"):
                    rel_no_ext = rel_no_ext[:-5]
                rel_no_ext_to_path.setdefault(rel_no_ext.lower(), rp)
            except Exception:
                pass

        cache = {
            "folder": folder_key,
            "all_java_files": all_java_files,
            "stem_to_paths": stem_to_paths,
            "rel_no_ext_to_path": rel_no_ext_to_path,
        }
        self._java_file_index_cache = cache
        return cache

    def _build_method_index_map(self, java_text: str):
        """Map of (start_pos, method_name) tuples sorted by position."""
        res = []
        for m in re_method_decl.finditer(java_text):
            res.append((m.start(), m.group(1)))
        res.sort(key=lambda x: x[0])
        return res

    def _find_enclosing_method(self, method_index_map, pos):
        candidate = None
        for start, name in method_index_map:
            if start <= pos:
                candidate = name
            else:
                break
        return candidate

    def _extract_values_with_vars(self, java_text: str):
        results = []
        for m in re_value_spel_dollar.finditer(java_text):
            key = m.group(1)
            span_end = m.end()
            var = None
            m2 = re_field_decl.search(java_text, span_end)
            if m2:
                var = m2.group(1)
            results.append({
                "Annotation": "@Value", "Property": key,
                "Variable": var, "span_start": m.start(), "span_end": span_end
            })

        for m in re_value_dollar.finditer(java_text):
            key = m.group(1)
            span_end = m.end()
            var = None
            m2 = re_field_decl.search(java_text, span_end)
            if m2:
                var = m2.group(1)
            results.append({
                "Annotation": "@Value", "Property": key,
                "Variable": var, "span_start": m.start(), "span_end": span_end
            })

        return results

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def file_extension(self) -> str:
        ext = self.details.get("extension")
        if isinstance(ext, str) and ext.strip():
            return ext.strip()
        return ".java"

    def _rx(self, key: str, flags: int = 0):
        pat = self.regex.get(key)
        if not isinstance(pat, str):
            raise KeyError(f"Regex key '{key}' missing or not a string")
        unesc = html.unescape(pat)
        try:
            return re.compile(unesc, flags)
        except re.error as err:
            raise re.error(
                f"[regex compile] key='{key}' pattern='{unesc}' error={err}"
            ) from err

    # ------------------------------------------------------------------
    # AST Parsing  (javalang targets Java 8 — no extra pre-processing needed)
    # ------------------------------------------------------------------

    def parse_ast(self, code: str):
        """
        Parse Java 8 source.  javalang handles all Java 8 features natively
        (lambdas, streams, default interface methods, diamond operator, etc.).
        Returns the compilation unit tree, or None on failure.
        """
        try:
            return javalang.parse.parse(code)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Type helpers
    # ------------------------------------------------------------------

    def _fqn_type_name(self, type_obj_or_str) -> str:
        """Return the fully-qualified type name by walking javalang sub_type chains.

        When Java source declares a variable with a package-qualified type like
        ``nl.row.path.ClassName obj``, javalang represents the type as a nested
        chain of ReferenceType nodes:
            ReferenceType(name='nl',
                sub_type=ReferenceType(name='row',
                    sub_type=ReferenceType(name='path',
                        sub_type=ReferenceType(name='ClassName'))))

        ``_simple_type_name`` only reads ``type.name`` (the root segment, 'nl')
        and then calls ``.split('.')[-1]`` — which returns ``'nl'``, not
        ``'ClassName'``.  This method walks the full chain and returns
        ``'nl.row.path.ClassName'`` so callers can store the FQN in
        object_class_map and let _resolve_fqn_path pick the correct file.
        """
        if type_obj_or_str is None:
            return None
        if not hasattr(type_obj_or_str, "name"):
            return str(type_obj_or_str)
        parts = [type_obj_or_str.name]
        sub = getattr(type_obj_or_str, "sub_type", None)
        while sub is not None:
            parts.append(sub.name)
            sub = getattr(sub, "sub_type", None)
        return ".".join(parts)

    def _effective_type_name(self, type_obj_or_str) -> str:
        """Return the best type name for storing in object_class_map.

        For a simple type like ``ClassName``, returns ``'ClassName'`` (same as
        _simple_type_name).

        For a package-qualified FQN like ``nl.row.path.ClassName``, returns the
        full FQN ``'nl.row.path.ClassName'`` so that _enrich_call_with_path can
        route it through _resolve_fqn_path and pick the correct source file even
        when multiple modules have a class with the same simple name.

        The key insight: javalang represents ``nl.row.path.ClassName`` as a
        chain of ReferenceType nodes linked via ``sub_type``.  Only by walking
        the whole chain (via _fqn_type_name) can we recover the full FQN;
        reading only ``type.name`` gives the root segment ``'nl'``.
        """
        fqn = self._fqn_type_name(type_obj_or_str)
        if not fqn:
            return None
        # Strip HTML-escaped generics
        fqn = re.sub(r'\s*&amp;amp;lt;[^&amp;amp;gt]+&amp;amp;gt;\s*', '', fqn)
        fqn = re.sub(r'\s*&lt;[^&gt;]+&gt;\s*', '', fqn)
        # If the FQN starts with a lowercase segment (package-qualified), preserve
        # the full FQN so _resolve_class_path → _resolve_fqn_path can use it.
        # Otherwise (simple class name or already-simple) return the last segment.
        if fqn and fqn[0].islower() and '.' in fqn:
            return fqn  # e.g. 'nl.row.path.ClassName'
        return fqn.split('.')[-1]  # e.g. 'ClassName' or 'List'

    def _simple_type_name(self, type_obj_or_str):
        if type_obj_or_str is None:
            return None
        # Walk sub_type chain first to get the correct simple name when the
        # declared type is a package-qualified FQN (e.g. nl.row.path.ClassName).
        # javalang stores the root package segment in type.name and nests the
        # rest via sub_type — so type.name alone gives 'nl', not 'ClassName'.
        fqn = self._fqn_type_name(type_obj_or_str)
        if fqn is None:
            return None
        # Strip HTML-escaped generics
        n = re.sub(r'\s*&amp;amp;lt;[^&amp;amp;gt]+&amp;amp;gt;\s*', '', fqn)
        n = re.sub(r'\s*&lt;[^&gt;]+&gt;\s*', '', n)
        return n.split('.')[-1]

    def _extract_method_annotations(self, method_node) -> str:
        ann_list = []
        if hasattr(method_node, "annotations") and method_node.annotations:
            for ann in method_node.annotations:
                try:
                    ann_list.append("@" + (ann.name if hasattr(ann, "name") else str(ann)))
                except Exception:
                    continue
        return ", ".join(ann_list) if ann_list else ""

    def _extract_method_declaration_type(self, method_node) -> str:
        if hasattr(method_node, "modifiers") and method_node.modifiers:
            mods = {m.lower() for m in method_node.modifiers}
            if "public" in mods:
                return "Public"
            if "private" in mods:
                return "Private"
            if "protected" in mods:
                return "Protected"
        return "Default"

    def _extract_return_type(self, method_node) -> str:
        try:
            rt = method_node.return_type
            if rt is None:
                return "void"
            base = rt.name if hasattr(rt, "name") else "Unknown"
            if hasattr(rt, "arguments") and rt.arguments:
                args = []
                for arg in rt.arguments:
                    if hasattr(arg, "type") and hasattr(arg.type, "name"):
                        args.append(arg.type.name)
                    elif hasattr(arg, "name"):
                        args.append(arg.name)
                return f"{base}&amp;lt;{', '.join(args)}&amp;gt;"
            return base
        except Exception:
            return "Unknown"

    def _type_to_simple(self, t) -> str:
        if t is None:
            return ""
        base = getattr(t, "name", str(t)) or ""
        if "." in base:
            base = base.split(".")[-1]
        dims = "[]" * int(getattr(t, "dimensions", 0) or 0)
        return f"{base}{dims}"

    def extract_method_metadata(self, method_node) -> dict:
        is_ctor = isinstance(method_node, jt.ConstructorDeclaration)

        param_types = []
        for p in getattr(method_node, "parameters", []) or []:
            param_type = getattr(p, "type", None)
            t = self._effective_type_name(param_type) or ""
            t += "[]" * int(getattr(param_type, "dimensions", 0) or 0)
            if getattr(p, "varargs", False):
                t = t + "[]"
            param_types.append(t)

        return {
            "Annotations": self._extract_method_annotations(method_node),
            "Method_Declaration_Type": self._extract_method_declaration_type(method_node),
            "return_type": "constructor" if is_ctor else self._extract_return_type(method_node),
            "member_kind": "Constructor" if is_ctor else "Method",
            "Parameters": ", ".join(param_types),
            "Parameter_Arity": len(param_types),
            "Parameter_Types": ";".join(param_types),
        }

    # ------------------------------------------------------------------
    # Import helpers
    # ------------------------------------------------------------------

    def _collect_com_imports(self, tree):
        imports_types = set()
        wildcard_packages = set()
        static_members = set()
        static_wildcard_classes = set()

        for imp in getattr(tree, "imports", []):
            path = getattr(imp, "path", "")
            if not isinstance(path, str) or not path.startswith("nl."):
                continue
            parts = [p for p in path.split('.') if p]
            if getattr(imp, "static", False):
                if parts[-1] == '*':
                    if len(parts) >= 2:
                        static_wildcard_classes.add(parts[-2])
                else:
                    static_members.add(parts[-1])
                    if len(parts) >= 2:
                        imports_types.add(parts[-2])
            else:
                if parts[-1] == '*':
                    wildcard_packages.add('.'.join(parts[:-1]))
                else:
                    imports_types.add(parts[-1])

        return imports_types, wildcard_packages, static_members, static_wildcard_classes

    # ------------------------------------------------------------------
    # Field / DI helpers
    # ------------------------------------------------------------------

    def _collect_autowired_fields(self, class_node) -> dict:
        """Collect ALL instance field declarations (not just @Autowired/@Inject).

        Plain private fields like:
            private RequestDetailsValidator requestDetailsValidator;
            private MultiSortPagingContextValidator pagingContextValidator;
        must be included so that calls like:
            this.requestDetailsValidator.validate(...)
            this.pagingContextValidator.setMaxPageSize(...)
        pass _keep_qualified_call (which checks `if qual in autowired_fields`)
        and resolve to the correct class name rather than the bare variable name.
        """
        autowired = {}
        for _, fd in class_node.filter(jt.FieldDeclaration):
            tname = self._effective_type_name(fd.type)
            for decl in getattr(fd, "declarators", []):
                autowired[decl.name] = tname
        return autowired

    # ------------------------------------------------------------------
    # Variable type inference
    # ------------------------------------------------------------------

    def _infer_type_from_initializer(self, decl):
        init = getattr(decl, "initializer", None)
        try:
            if isinstance(init, jt.ClassCreator):
                # FIX: use _effective_type_name instead of _simple_type_name so that
                # a new-expression with a package-qualified type like
                #   new nl.acme.foo.ClassName()
                # preserves the full FQN 'nl.acme.foo.ClassName' in object_class_map /
                # var_map.  _simple_type_name strips all but the last segment, so the
                # FQN was lost and _resolve_class_path could not route through
                # _resolve_fqn_path → the import was effectively ignored.
                return self._effective_type_name(init.type)
        except Exception:
            pass
        return None

    def _build_var_types_for_method(self, method_node, autowired_fields):
        var_types = {}
        locals_from_new = set()
        params_set = set()

        for p in getattr(method_node, "parameters", []):
            # FIX: use _effective_type_name so that package-qualified parameter types
            # like  nl.rabobank.schemas...SearchOptions searchOptions  are stored as
            # the full FQN 'nl.rabobank.schemas...SearchOptions' rather than the
            # truncated simple name 'SearchOptions'.
            # Without this, when find_calls_in_method resolves searchOptions.method()
            # it emits 'SearchOptions.method()' which _enrich_call_with_path maps to
            # the wrong (ambiguous) class file found first in type_to_path_full.
            var_types[p.name] = self._effective_type_name(p.type)
            params_set.add(p.name)

        for _, lv in method_node.filter(jt.LocalVariableDeclaration):
            # FIX: use _effective_type_name for local variables too, for the same
            # reason: a variable declared as  nl.x.y.Foo f = ...  must be stored as
            # 'nl.x.y.Foo' so _resolve_class_path can route through _resolve_fqn_path.
            declared_type = self._effective_type_name(lv.type)
            for decl in getattr(lv, "declarators", []):
                name = decl.name
                tname = declared_type
                # Java 8 has no 'var'; skip the var-inference branch from Java 18 adapter
                inferred = self._infer_type_from_initializer(decl)
                if inferred:
                    # FIX: only use the inferred (new-expression) type when there is no
                    # declared type.  For  Class obj = new Class()  the declared_type is
                    # already 'Class' (with its import-resolvable simple name), so
                    # overwriting it with the inferred value was harmless but hid the case
                    # where declared_type carried a package-qualified FQN that inferred
                    # (coming from _simple_type_name before Fix 1) stripped away.
                    # Now that _infer_type_from_initializer uses _effective_type_name both
                    # values are equivalent for the FQN case, but keeping declared_type
                    # as the primary source is semantically correct (the compiler uses it).
                    if not tname:
                        tname = inferred
                    locals_from_new.add(name)
                var_types[name] = tname

        var_types.update(autowired_fields or {})
        return var_types, locals_from_new, params_set

    # ------------------------------------------------------------------
    # Call-filtering helpers
    # ------------------------------------------------------------------

    def _normalize_qualifier(self, qual: str, var_types: dict) -> str:
        if qual in var_types:
            return qual
        for k in var_types:
            if qual == (k + k):
                return k
        return qual

    def _is_same_package_type(self, type_name, package_name) -> bool:
        return bool(package_name and type_name)

    def _get_super_type_name(self, type_node) -> str:
        """Return the simple superclass name for the current type, if any."""
        try:
            parent = getattr(type_node, "extends", None)
            if parent is None:
                return None
            parent_name = self._simple_type_name(parent)
            if parent_name:
                return parent_name
            raw = getattr(parent, "name", None) or str(parent)
            if isinstance(raw, str) and raw.strip():
                return raw.strip().split('.')[-1]
        except Exception:
            pass
        return None

    def _keep_qualified_call(self, qual, var_types, imports_types, autowired_fields,
                              wildcard_packages, locals_from_new, params_set, package_name) -> bool:
        qual = self._normalize_qualifier(qual, var_types)
        if qual in autowired_fields:
            return True
        t = var_types.get(qual)
        if t and qual in locals_from_new and self.accept_local_new_types:
            return True
        if t and qual in params_set and self.accept_parameter_types:
            return True
        if t and t in imports_types:
            return True
        if qual in imports_types:
            return True
        if wildcard_packages and t:
            return True
        if self.accept_same_package and self._is_same_package_type(t, package_name):
            return True
        if t:          # variable has a known declared type in var_types
            return True
        return False
    def _keep_unqualified_call(self, member, static_members, static_wildcard_classes) -> bool:
        if self.include_unqualified:
            return True
        if member in static_members:
            return True
        if static_wildcard_classes:
            return True
        return False

    def _get_package_name(self, java_code: str):
        pat = html.unescape(
            self.regex.get("package", r'^\s*package\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*;')
        )
        m = re.search(pat, java_code, flags=re.MULTILINE)
        return m.group(1) if m else None

    # ------------------------------------------------------------------
    # Declared types
    # ------------------------------------------------------------------

    def get_declared_types(self, ast):
        """
        Yield (name, kind, node) for classes, interfaces, and enums.
        Java 8 does NOT have records or sealed classes — those are omitted.
        """
        types = []

        # Classes
        for _, cls in ast.filter(jt.ClassDeclaration):
            types.append((getattr(cls, "name", "Unknown"), "class", cls))

        # Interfaces (including those with default/static methods — Java 8)
        for _, ifc in ast.filter(jt.InterfaceDeclaration):
            types.append((getattr(ifc, "name", "Unknown"), "interface", ifc))

        # Enums
        for _, en in ast.filter(jt.EnumDeclaration):
            types.append((getattr(en, "name", "Unknown"), "enum", en))

        return types

    def get_methods_in_type(self, type_node):
        """
        Yield (name, node) for every method and constructor in type_node.
        For interfaces, this includes default and static methods (Java 8+).
        """
        # Iterate only direct members declared in this type body.
        # Using recursive filter() can mix nested-type members into the
        # parent type and misattribute calls/metadata (e.g. constructor tags).
        for member in getattr(type_node, "body", []) or []:
            if isinstance(member, jt.MethodDeclaration):
                yield member.name, member
            elif isinstance(member, jt.ConstructorDeclaration):
                yield member.name, member

    # ------------------------------------------------------------------
    # Method source extraction
    # ------------------------------------------------------------------

    # Cache of code-id → cumulative line offsets so we only build it once per file
    _line_offset_cache: dict = {}

    def _get_line_offsets(self, code: str, lines) -> list:
        """Return cumulative byte offsets for each line (cached per code object)."""
        key = id(code)
        cached = self._line_offset_cache.get(key)
        if cached is not None:
            return cached
        offsets = [0] * (len(lines) + 1)
        for i, ln in enumerate(lines):
            offsets[i + 1] = offsets[i] + len(ln)
        self._line_offset_cache[key] = offsets
        # Evict old entries to cap memory (keep last 8 files)
        if len(self._line_offset_cache) > 8:
            oldest = next(iter(self._line_offset_cache))
            del self._line_offset_cache[oldest]
        return offsets

    def _get_method_source(self, code: str, method_node):
        try:
            lines = code.splitlines(True)
            if hasattr(method_node, "position") and method_node.position and method_node.position[0]:
                start_line = method_node.position[0] - 1
                offsets = self._get_line_offsets(code, lines)
                start_offset = offsets[start_line]
                start_brace_idx = code.find('{', start_offset)
                if start_brace_idx == -1:
                    return None
                brace_count = 0
                end_idx = None
                for i in range(start_brace_idx, len(code)):
                    ch = code[i]
                    if ch == '{':
                        brace_count += 1
                    elif ch == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            end_idx = i
                            break
                return code[start_brace_idx:end_idx + 1] if end_idx is not None else None
            else:
                mname = getattr(method_node, "name", None)
                if not mname:
                    return None
                # Cache compiled patterns — same method name reused across many calls
                _sig_cache = getattr(self, '_sig_pat_cache', None)
                if _sig_cache is None:
                    self._sig_pat_cache = {}
                    _sig_cache = self._sig_pat_cache
                sig_pat = _sig_cache.get(mname)
                if sig_pat is None:
                    sig_pat = re.compile(
                        r'\b' + re.escape(mname) + r'\s*\([^)]*\)\s*\{',
                        re.MULTILINE | re.DOTALL
                    )
                    _sig_cache[mname] = sig_pat
                match = sig_pat.search(code)
                if not match:
                    return None
                start_brace_idx = match.end() - 1
                brace_count = 0
                end_idx = None
                for i in range(start_brace_idx, len(code)):
                    ch = code[i]
                    if ch == '{':
                        brace_count += 1
                    elif ch == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            end_idx = i
                            break
                return code[start_brace_idx:end_idx + 1] if end_idx is not None else None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Dynamic qualifier / chained-call extraction
    # ------------------------------------------------------------------

    def _extract_dynamic_terminal_methods(self, text: str) -> set:
        """
        Extract terminal method names from dynamic chains such as
        map.get(key).doSomething(...).
        Java 8 streams produce many such patterns.
        """
        if not isinstance(text, str) or not text.strip():
            return set()

        pat = None
        if isinstance(self.regex.get("re_dynamic_qual"), str) and self.regex["re_dynamic_qual"].strip():
            try:
                pat = self._rx("re_dynamic_qual", flags=re.MULTILINE | re.DOTALL)
            except Exception:
                pat = None

        terms = set()
        if pat is not None:
            for m in pat.finditer(text):
                try:
                    name = m.group(1)
                    if isinstance(name, str) and name.strip():
                        terms.add(f"{name.strip()}()")
                except Exception:
                    continue
            return terms

        # Default single-dynamic-segment: base(...).terminal(...)
        single_dyn = re.compile(
            r"""\b[A-Za-z_]\w*\s*\([^()]*\)\s*\.\s*([A-Za-z_]\w*)\s*\(""",
            re.MULTILINE | re.DOTALL,
        )
        for m in single_dyn.finditer(text):
            name = m.group(1)
            if name and name.strip():
                terms.add(f"{name.strip()}()")

        # Multi-segment chains: base(...).m1(...).m2(...)
        chain_dyn = re.compile(
            r"""\b[A-Za-z_]\w*\s*\([^()]*\)(?:\s*\.\s*[A-Za-z_]\w*\s*\([^()]*\))+""",
            re.MULTILINE | re.DOTALL,
        )
        for cm in chain_dyn.finditer(text):
            last_methods = re.findall(r'\.\s*([A-Za-z_]\w*)\s*\(', cm.group(0))
            if last_methods:
                terms.add(f"{last_methods[-1].strip()}()")

        return terms

    def _is_enum_runtime_accessor(self, qualifier: str, member: str) -> bool:
        """True for enum runtime accessor calls like Status.OUTSTANDING.name()."""
        if not isinstance(member, str) or member not in {"name", "ordinal"}:
            return False
        if not isinstance(qualifier, str):
            return False
        q = qualifier.strip()
        if not q or "(" in q or ")" in q:
            return False
        parts = [p for p in q.split('.') if p]
        if not parts:
            return False
        tail = parts[-1]
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*', tail):
            return False
        return len(parts) == 1 or (parts[0] and parts[0][0].isupper())

    def _is_enum_runtime_accessor_call(self, call: str) -> bool:
        if not isinstance(call, str):
            return False
        m = re.match(r'^\s*([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\.([A-Za-z_]\w*)\s*\(\s*\)\s*$', call)
        if not m:
            return False
        return self._is_enum_runtime_accessor(m.group(1), m.group(2))

    # ------------------------------------------------------------------
    # Expression walker (for nested invocations)
    # ------------------------------------------------------------------

    def _collect_invocations_in_expression(
        self, expr, *,
        var_types, imports_types, autowired_fields,
        wildcard_packages, locals_from_new, params_set, package_name,
    ) -> set:
        calls = set()
        if expr is None:
            return calls
        try:
            if isinstance(expr, jt.MethodInvocation):
                qual = expr.qualifier or ""
                member = expr.member
                if qual and self._is_enum_runtime_accessor(qual, member):
                    return calls
                if qual and ("(" in qual or ")" in qual):
                    calls.add(f"{qual}.{member}()")
                elif qual:
                    qual = self._normalize_qualifier(qual, var_types)
                    if self._keep_qualified_call(
                        qual, var_types, imports_types, autowired_fields,
                        wildcard_packages, locals_from_new, params_set, package_name,
                    ):
                        resolved_type = var_types.get(qual) or autowired_fields.get(qual) or qual
                        # FIX: emit simple class name when resolved_type is an FQN
                        if isinstance(resolved_type, str) and '.' in resolved_type and resolved_type[0].islower():
                            resolved_type = resolved_type.split('.')[-1]
                        calls.add(f"{resolved_type}.{member}()")
                else:
                    if self._keep_unqualified_call(member, set(), set()):
                        calls.add(f"{member}()")
                for a in getattr(expr, "arguments", []) or []:
                    calls |= self._collect_invocations_in_expression(
                        a, var_types=var_types, imports_types=imports_types,
                        autowired_fields=autowired_fields, wildcard_packages=wildcard_packages,
                        locals_from_new=locals_from_new, params_set=params_set,
                        package_name=package_name,
                    )
                return calls

            if isinstance(expr, jt.ClassCreator):
                ctor_type = self._simple_type_name(expr.type)
                ctor_args = getattr(expr, "arguments", []) or []
                if ctor_type and ctor_args:
                    calls.add(f"{ctor_type}.{ctor_type}()")
                for a in ctor_args:
                    calls |= self._collect_invocations_in_expression(
                        a, var_types=var_types, imports_types=imports_types,
                        autowired_fields=autowired_fields, wildcard_packages=wildcard_packages,
                        locals_from_new=locals_from_new, params_set=params_set,
                        package_name=package_name,
                    )
                return calls

            for attr in ("expression", "condition", "then_expression", "else_expression",
                         "left", "right", "operand"):
                node = getattr(expr, attr, None)
                if node is not None:
                    calls |= self._collect_invocations_in_expression(
                        node, var_types=var_types, imports_types=imports_types,
                        autowired_fields=autowired_fields, wildcard_packages=wildcard_packages,
                        locals_from_new=locals_from_new, params_set=params_set,
                        package_name=package_name,
                    )
            for list_attr in ("expressions", "arguments"):
                lst = getattr(expr, list_attr, None)
                if isinstance(lst, (list, tuple)):
                    for node in lst:
                        calls |= self._collect_invocations_in_expression(
                            node, var_types=var_types, imports_types=imports_types,
                            autowired_fields=autowired_fields, wildcard_packages=wildcard_packages,
                            locals_from_new=locals_from_new, params_set=params_set,
                            package_name=package_name,
                        )
        except Exception:
            pass
        return calls

    # ------------------------------------------------------------------
    # Main call-finder (AST path)
    # ------------------------------------------------------------------

    def find_calls_in_method(self, type_node, method_node, code: str) -> list:
        calls = set()
        package_name = self._get_package_name(code)
        super_type_name = self._get_super_type_name(type_node)

        # FIX 1 & 3: Re-use cached AST instead of re-parsing the full file
        # on every method call.  _raw_ast_cache is injected by configure().
        _cache_key = id(code)  # code object is the same str within one file run
        _cached = self._raw_ast_cache.get(_cache_key)
        if _cached is None:
            try:
                _cached = javalang.parse.parse(code)
            except Exception:
                _cached = False  # sentinel: parse failed
            self._raw_ast_cache[_cache_key] = _cached

        try:
            if _cached and _cached is not False:
                tree = _cached
            else:
                tree = javalang.parse.parse(code)
            imports_types, wildcard_packages, static_members, static_wildcard_classes = \
                self._collect_com_imports(tree)
        except Exception:
            imports_types, wildcard_packages, static_members, static_wildcard_classes = \
                set(), set(), set(), set()

        autowired_fields = self._collect_autowired_fields(type_node)
        var_types, locals_from_new, params_set = self._build_var_types_for_method(
            method_node, autowired_fields
        )

        # Include for-loop element variables
        # FIX: use _effective_type_name so FQN types are preserved here too
        for _, forstmt in method_node.filter(jt.ForStatement):
            if hasattr(forstmt, "control") and hasattr(forstmt.control, "var"):
                var_decl = forstmt.control.var
                if var_decl:
                    tname = self._effective_type_name(var_decl.type)
                    for declarator in getattr(var_decl, "declarators", []):
                        var_types[declarator.name] = tname

        def _is_dynamic(q: str) -> bool:
            return isinstance(q, str) and ("(" in q or ")" in q)

        # Pre-build set of MethodInvocations that are selectors on a ClassCreator
        # or another MethodInvocation — they have qualifier=None but are NOT sibling calls.
        _selector_invocations = set()
        
        def _collect_selector_ids(node, result_set):
            for sel in (getattr(node, "selectors", None) or []):
                if isinstance(sel, jt.MethodInvocation):
                    result_set.add(id(sel))
                    _collect_selector_ids(sel, result_set)  # recurse into nested selectors

        _selector_invocations = set()
        for _, cc in method_node.filter(jt.ClassCreator):
            _collect_selector_ids(cc, _selector_invocations)
        for _, parent_inv in method_node.filter(jt.MethodInvocation):
            _collect_selector_ids(parent_inv, _selector_invocations)
        def _build_chain_string(start_inv, resolved_root):
            """Build full chain string including selectors.
            e.g. abc.method1() with selector method2() → 'ABC.method1().method2()'
            """
            chain = f"{resolved_root}.{start_inv.member}()"
            for sel in (start_inv.selectors or []):
                if isinstance(sel, jt.MethodInvocation):
                    chain += f".{sel.member}()"
            return chain

        def _emit_chain_segments(start_inv, resolved_root, calls_set):
            """FIX 2: emit EACH segment of a chained call individually.

            For  a.method1().method2()  where 'a' resolves to 'ClassA':
              - emits "ClassA.method1()"  (always — we know this class)
              - emits full chain "ClassA.method1().method2()" for downstream
                resolution in case the cleaner can resolve method2's class.

            This ensures method1 is never lost even when method2's return
            type is missing from method_return_index.
            """
            # Always emit the root segment independently
            calls_set.add(f"{resolved_root}.{start_inv.member}()")
            # Record every method name emitted with a real class prefix so the
            # regex fallback path can suppress the bare unqualified duplicates.
            _ast_qualified_methods.add(start_inv.member)
            # Also emit the full chain so the cleaner can resolve downstream
            selectors = [s for s in (start_inv.selectors or [])
                         if isinstance(s, jt.MethodInvocation)]
            if selectors:
                full_chain = f"{resolved_root}.{start_inv.member}()"
                for sel in selectors:
                    full_chain += f".{sel.member}()"
                    _ast_qualified_methods.add(sel.member)
                calls_set.add(full_chain)

        # Pre-build sibling set ONCE (was rebuilt inside every MethodInvocation iteration)
        _sibling_method_names = {n for n, _ in self.get_methods_in_type(type_node)}
        _type_class_name = getattr(type_node, "name", None)

        # Track every method name the AST path emits with a resolved class prefix
        # (e.g. "RequestorValidator.validate") so the regex fallback path below
        # does not re-emit them as bare unqualified calls ("validate", "setMaxPageSize")
        # which the cleaner cannot attribute and which become spurious output rows.
        _ast_qualified_methods: set = set()

        # AST: MethodInvocation nodes
        for _, inv in method_node.filter(jt.MethodInvocation):
            qual = inv.qualifier or ""
            member = inv.member
            if qual and self._is_enum_runtime_accessor(qual, member):
                continue

            if not qual:
                # If this is a selector on a ClassCreator or chained call,
                # it is handled by its parent — skip to avoid wrong class attribution.
                if id(inv) in _selector_invocations:
                    pass
                else:
                    sibling_method_names = _sibling_method_names
                    if member in sibling_method_names:
                        class_name = _type_class_name
                        if class_name:
                            # FIX 2: emit each chain segment independently
                            _emit_chain_segments(inv, class_name, calls)
                        else:
                            calls.add(f"{member}()")
                    elif self._keep_unqualified_call(member, static_members, static_wildcard_classes):
                        calls.add(f"{member}()")
            elif qual == "super":
                # Convert super.method(...) into ParentClass.method(...)
                # so inherited implementations can be resolved downstream.
                if super_type_name:
                    _emit_chain_segments(inv, super_type_name, calls)
                else:
                    calls.add(f"{member}()")
            elif _is_dynamic(qual):
                calls.add(f"{qual}.{member}()")
            else:
                # Strip "this." prefix so "this.obj" resolves the same as "obj"
                if qual.startswith("this."):
                    qual = qual[5:]
                qual = self._normalize_qualifier(qual, var_types)
                if self._keep_qualified_call(
                    qual, var_types, imports_types, autowired_fields,
                    wildcard_packages, locals_from_new, params_set, package_name,
                ):
                    # Resolve the variable name to its declared class.
                    # var_types covers local variables and parameters;
                    # autowired_fields covers all field declarations (injected or plain private).
                    # Without the autowired_fields fallback, plain private fields like
                    #   private RequestDetailsValidator requestDetailsValidator;
                    # resolve to the bare variable name ("requestDetailsValidator")
                    # instead of the class name ("RequestDetailsValidator"), producing
                    # a lowercase-rooted call that the cleaner silently drops.
                    resolved_type = var_types.get(qual) or autowired_fields.get(qual) or qual
                    # FIX: var_types may now store a package-qualified FQN such as
                    # 'nl.rabobank.schemas.cs.rom.extendedquerypaymentorder._1.req.SearchOptions'
                    # for a parameter declared as:
                    #   final nl.rabobank.schemas...SearchOptions searchOptions
                    # Emitting the full FQN as the call root produces
                    #   'nl.rabobank.schemas...SearchOptions.isOnlyBatches()'
                    # which _enrich_call_with_path cannot parse (its regex only grabs
                    # the first dot-segment 'nl' as the class name).
                    # We must emit ONLY the simple class name (last FQN segment) so
                    # the downstream enrichment step sees 'SearchOptions.isOnlyBatches()'
                    # and can then resolve it correctly via _resolve_class_path /
                    # _resolve_fqn_path using the import-aware caller-file var_map.
                    if isinstance(resolved_type, str) and '.' in resolved_type and resolved_type[0].islower():
                        resolved_type = resolved_type.split('.')[-1]
                    _emit_chain_segments(inv, resolved_type, calls)

        # ---------------------------------------------------------------
        # Handle this.field.method() calls — javalang parses these as a
        # This node with selectors, NOT as a MethodInvocation with a
        # qualifier.  The MethodInvocation loop above never sees them.
        #
        # AST shape for  this.requestorValidator.validate(...) :
        #   This(selectors=[
        #       MemberReference(member="requestorValidator"),
        #       MethodInvocation(member="validate", qualifier=None)
        #   ])
        #
        # We walk every This node in the method body, find the first
        # MemberReference selector (the field name), resolve it to its
        # declared class via autowired_fields / var_types, then emit
        # every subsequent MethodInvocation selector as a qualified call.
        # ---------------------------------------------------------------
        for _, this_node in method_node.filter(jt.This):
            selectors = getattr(this_node, "selectors", None) or []
            if not selectors:
                continue

            # First selector must be a MemberReference — that is the field name
            # e.g. "requestorValidator", "requestDetailsValidator"
            first = selectors[0]
            if not isinstance(first, jt.MemberReference):
                continue
            field_name = first.member

            # Resolve field name → declared class name
            resolved_class = (
                autowired_fields.get(field_name)
                or var_types.get(field_name)
                or field_name   # last resort: keep as-is (will be lowercase, cleaner drops it)
            )
            # FIX: same FQN → simple name conversion as in the MethodInvocation loop above.
            # autowired_fields may store a package-qualified FQN for fields declared with
            # fully-qualified types.  Emit only the simple name so _enrich_call_with_path
            # can parse and resolve the call correctly.
            if isinstance(resolved_class, str) and '.' in resolved_class and resolved_class[0].islower():
                resolved_class = resolved_class.split('.')[-1]

            # Walk remaining selectors — emit every MethodInvocation
            for sel in selectors[1:]:
                if isinstance(sel, jt.MethodInvocation):
                    calls.add(f"{resolved_class}.{sel.member}()")
                    _ast_qualified_methods.add(sel.member)
                elif isinstance(sel, jt.MemberReference):
                    # Further field chaining (rare): update resolved class via
                    # method_return_index if available, otherwise skip.
                    pass  # future: chain through method_return_index

        # Collect ClassCreator IDs already handled via ThrowStatement
        _throw_creators = set()
        for _, th in method_node.filter(jt.ThrowStatement):
            expr = getattr(th, "expression", None)
            if isinstance(expr, jt.ClassCreator):
                _throw_creators.add(id(expr))

        # Standalone new X(...) — not inside a throw
        for _, cc in method_node.filter(jt.ClassCreator):
            if id(cc) in _throw_creators:
                continue
            ctor_type = self._simple_type_name(cc.type)
            if not ctor_type:
                continue
            ctor_args = getattr(cc, "arguments", []) or []
            has_ctor_args = len(ctor_args) > 0
            if ctor_type in imports_types or wildcard_packages or \
               self.accept_local_new_types or self.accept_same_package:
                if has_ctor_args:
                    calls.add(f"{ctor_type}.{ctor_type}()")
            for arg in ctor_args:
                calls |= self._collect_invocations_in_expression(
                    arg, var_types=var_types, imports_types=imports_types,
                    autowired_fields=autowired_fields, wildcard_packages=wildcard_packages,
                    locals_from_new=locals_from_new, params_set=params_set,
                    package_name=package_name,
                )

        # throw new Type(...) — constructors + nested calls
        
        for _, th in method_node.filter(jt.ThrowStatement):
            expr = getattr(th, "expression", None)
            if isinstance(expr, jt.ClassCreator) and getattr(expr, "type", None):
                ctor_type = self._simple_type_name(expr.type)
                ctor_args = getattr(expr, "arguments", []) or []
                has_ctor_args = len(ctor_args) > 0
                if ctor_type and has_ctor_args:
                    calls.add(f"{ctor_type}.{ctor_type}()")
            calls |= self._collect_invocations_in_expression(
                expr, var_types=var_types, imports_types=imports_types,
                autowired_fields=autowired_fields, wildcard_packages=wildcard_packages,
                locals_from_new=locals_from_new, params_set=params_set,
                package_name=package_name,
            )

        # # Chained / stream calls via method source regex
        # chained_pat = re.compile(
        #     r'''\b[a-zA-Z_]\w*(?:\s*\([^()]*\))?(?:\s*\.\s*[a-zA-Z_]\w*\s*\([^()]*\)){1,}''',
        #     re.MULTILINE | re.VERBOSE,
        # )
        # src = self._get_method_source(code, method_node)
        # if src:
        #     src_clean = _strip_source_comments(src)
        #     for chain in chained_pat.findall(src_clean):
        #         chain = chain.strip()
        #         if chain:
        #             calls.add(chain)
        #     for dyn in self._extract_dynamic_terminal_methods(src_clean):
        #         calls.add(dyn)

        # Chained / stream calls via method source regex
        chained_pat = re.compile(
            r'''\b[a-zA-Z_]\w*(?:\s*\([^()]*\))?(?:\s*\.\s*[a-zA-Z_]\w*\s*\([^()]*\)){1,}''',
            re.MULTILINE | re.VERBOSE,
        )
        # Matches chains rooted at a constructor call: Word(...).method(...)
        # e.g. "BigDecimal(quantity).multiply(price)" — the root is a ctor, not a var/class.
        ctor_rooted_pat = re.compile(r'^([A-Za-z_]\w*)\s*\(')
        leading_var_pat = re.compile(r'^([A-Za-z_]\w*)\.')
        java_kw = self.language_keywords()
        src = self._get_method_source(code, method_node)
        # Track method names claimed by chained_pat so _extract_dynamic_terminal_methods
        # does not re-emit them as bare unqualified calls (which get attributed to this class).
        chained_claimed_methods: set = set()
        if src:
            src_clean = _strip_source_comments(src)
            for chain in chained_pat.findall(src_clean):
                chain = chain.strip()
                if not chain:
                    continue
                lv = leading_var_pat.match(chain)
                if lv:
                    leading = lv.group(1)
                    # Skip chains rooted at a Java keyword (return, new, etc.)
                    if leading in java_kw:
                        continue
                    resolved = var_types.get(leading)
                    if resolved and resolved != leading:
                        # Known variable — replace with its resolved type name.
                        # FIX: if the stored type is a package-qualified FQN (first
                        # char lowercase, e.g. 'nl.path.SearchOptions'), use only
                        # the simple class name (last segment) for the call string.
                        # The downstream enrichment (_enrich_call_with_path) reads
                        # _build_var_map from the source file which does the FQN-to-
                        # path mapping; emitting the full FQN here would break the
                        # regex that extracts the class token from the call string.
                        if isinstance(resolved, str) and '.' in resolved and resolved[0].islower():
                            resolved = resolved.split('.')[-1]
                        chain = resolved + chain[len(leading):]
                        calls.add(chain)
                    elif leading in var_types:
                        # Known variable whose name matches its type
                        calls.add(chain)
                    elif leading[0].isupper():
                        # Looks like a class name (UpperCamelCase) — keep as-is
                        calls.add(chain)
                    else:
                        # Lowercase token not in var_types — try autowired/private fields.
                        # e.g. "requestorValidator.validate(...)" where requestorValidator
                        # is a plain private field (not @Autowired) lives in autowired_fields.
                        field_type = autowired_fields.get(leading)
                        if field_type:
                            # FIX: same FQN → simple name conversion for field types
                            if isinstance(field_type, str) and '.' in field_type and field_type[0].islower():
                                field_type = field_type.split('.')[-1]
                            chain = field_type + chain[len(leading):]
                            calls.add(chain)
                        # else: truly unknown — skip to avoid false attribution
                else:
                    # No leading "Word." prefix.  This happens for constructor-rooted
                    # chains like "BigDecimal(quantity).multiply(price)" where the
                    # token before the first "(" is the type name, not a variable.
                    # Rewrite as "TypeName.method1().method2()..." so the call is
                    # attributed to the right type rather than added as a raw string
                    # (which downstream code cannot parse) or dropped silently.
                    cr = ctor_rooted_pat.match(chain)
                    if cr:
                        ctor_type = cr.group(1)
                        if ctor_type not in java_kw:
                            # Extract every .method() segment after the constructor call.
                            segments = re.findall(r'\.\s*([A-Za-z_]\w*)\s*\(', chain)
                            for seg in segments:
                                rewritten = f"{ctor_type}.{seg}()"
                                calls.add(rewritten)
                                chained_claimed_methods.add(seg)
                    # else: truly unclassifiable — skip to avoid false attribution
            for dyn in self._extract_dynamic_terminal_methods(src_clean):
                # Strip trailing "()" to get the bare name for the duplicate check.
                bare = dyn[:-2] if dyn.endswith("()") else dyn
                if bare in chained_claimed_methods:
                    # chained_pat already emitted a properly qualified version;
                    # the bare unqualified form would be attributed to this class — skip.
                    continue
                if bare in _ast_qualified_methods:
                    # The AST path already emitted this method with its correct class prefix
                    # (e.g. "RequestorValidator.validate"). Suppress the bare form here —
                    # it would produce a spurious row attributed to the wrong class.
                    continue
                calls.add(dyn)

        # Some javalang builds expose super calls as a separate node type.
        if super_type_name and hasattr(jt, "SuperMethodInvocation"):
            for _, sinv in method_node.filter(jt.SuperMethodInvocation):
                member = getattr(sinv, "member", None)
                if not member:
                    continue
                calls.add(f"{super_type_name}.{member}()")
                _ast_qualified_methods.add(member)

        # ==============================================================
        # NEW PATTERNS — added additively; no existing logic modified.
        # Each section is independently guarded and appends to `calls`.
        # ==============================================================

        # ------------------------------------------------------------------
        # Pattern: CatchClause Traversal
        # Walk every catch block so method calls on the exception variable
        # (e.g. catch(Exception e) { handle(e); }) are not missed.
        # javalang node: CatchClause
        # ------------------------------------------------------------------
        for _, catch_clause in method_node.filter(jt.CatchClause):
            catch_param = getattr(catch_clause, "parameter", None)
            catch_block = getattr(catch_clause, "block", None)
            if catch_block is None:
                continue
            # Build a tiny var_map for the catch parameter so qualified calls
            # like e.getMessage() are resolved to the declared exception type.
            catch_var_types = dict(var_types)
            if catch_param is not None:
                param_name = getattr(catch_param, "name", None)
                # CatchClause parameter may have multiple types (multi-catch)
                param_types_list = getattr(catch_param, "types", None)
                if param_types_list:
                    # Use the first type for resolution (all share the same var)
                    first_type = param_types_list[0] if param_types_list else None
                    if first_type:
                        type_name = self._simple_type_name(first_type) if hasattr(first_type, "name") else str(first_type)
                        if param_name and type_name:
                            catch_var_types[param_name] = type_name
                else:
                    param_type = getattr(catch_param, "type", None)
                    type_name = self._simple_type_name(param_type) if param_type is not None else None
                    if param_name and type_name:
                        catch_var_types[param_name] = type_name
            # Recursively collect invocations inside the catch block
            for stmt in (catch_block if isinstance(catch_block, (list, tuple)) else []):
                calls |= self._collect_invocations_in_expression(
                    stmt,
                    var_types=catch_var_types,
                    imports_types=imports_types,
                    autowired_fields=autowired_fields,
                    wildcard_packages=wildcard_packages,
                    locals_from_new=locals_from_new,
                    params_set=params_set,
                    package_name=package_name,
                )

        # ------------------------------------------------------------------
        # Pattern: Multi-Catch Handling
        # Capture each exception type in a multi-catch clause.
        # javalang node: CatchClause.parameter.types (multiple)
        # e.g. catch(IOException | SQLException e) { ... }
        # ------------------------------------------------------------------
        for _, catch_clause in method_node.filter(jt.CatchClause):
            catch_param = getattr(catch_clause, "parameter", None)
            if catch_param is None:
                continue
            param_types_list = getattr(catch_param, "types", None)
            if not param_types_list or len(param_types_list) < 2:
                continue
            param_name = getattr(catch_param, "name", None)
            # Register each exception type so method calls via the variable resolve
            for exc_type in param_types_list:
                exc_type_name = self._simple_type_name(exc_type) if hasattr(exc_type, "name") else str(exc_type)
                if param_name and exc_type_name:
                    # Record mapping for potential downstream resolution
                    var_types.setdefault(param_name, exc_type_name)

        # ------------------------------------------------------------------
        # Pattern: Try-With-Resources Traversal
        # Walk resource declarations and the try body.
        # javalang node: TryStatement.resources
        # e.g. try (Connection c = getConnection()) { ... }
        # ------------------------------------------------------------------
        for _, try_stmt in method_node.filter(jt.TryStatement):
            resources = getattr(try_stmt, "resources", None) or []
            for res in resources:
                # TryResource: type + name + value (initializer)
                res_type = getattr(res, "type", None)
                res_name = getattr(res, "name", None)
                res_value = getattr(res, "value", None)
                if res_type is not None and res_name:
                    tname = self._effective_type_name(res_type)
                    if tname:
                        var_types.setdefault(res_name, tname)
                # Collect any method call inside the resource initializer
                if res_value is not None:
                    calls |= self._collect_invocations_in_expression(
                        res_value,
                        var_types=var_types,
                        imports_types=imports_types,
                        autowired_fields=autowired_fields,
                        wildcard_packages=wildcard_packages,
                        locals_from_new=locals_from_new,
                        params_set=params_set,
                        package_name=package_name,
                    )

        # ------------------------------------------------------------------
        # Pattern: Explicit this() Constructor Delegation
        # Detect constructor-to-constructor calls within the same class.
        # javalang node: ExplicitConstructorInvocation (qualifier "this")
        # e.g. this(id);
        # ------------------------------------------------------------------
        for _, eci in method_node.filter(jt.ExplicitConstructorInvocation):
            qualifier = getattr(eci, "qualifier", None) or ""
            if str(qualifier).lower() == "this" or qualifier == "":
                # this(...) delegation — emit as same-class constructor call
                class_name = getattr(type_node, "name", None)
                if class_name:
                    calls.add(f"{class_name}.{class_name}()")
                    for arg in getattr(eci, "arguments", []) or []:
                        calls |= self._collect_invocations_in_expression(
                            arg,
                            var_types=var_types,
                            imports_types=imports_types,
                            autowired_fields=autowired_fields,
                            wildcard_packages=wildcard_packages,
                            locals_from_new=locals_from_new,
                            params_set=params_set,
                            package_name=package_name,
                        )

        # ------------------------------------------------------------------
        # Pattern: Explicit super() Constructor Delegation
        # Detect constructor-to-parent-constructor lineage.
        # javalang node: ExplicitConstructorInvocation / SuperConstructorInvocation
        # e.g. super(id);
        # ------------------------------------------------------------------
        # Handle via SuperConstructorInvocation when available
        if hasattr(jt, "SuperConstructorInvocation"):
            for _, sci in method_node.filter(jt.SuperConstructorInvocation):
                if super_type_name:
                    calls.add(f"{super_type_name}.{super_type_name}()")
                for arg in getattr(sci, "arguments", []) or []:
                    calls |= self._collect_invocations_in_expression(
                        arg,
                        var_types=var_types,
                        imports_types=imports_types,
                        autowired_fields=autowired_fields,
                        wildcard_packages=wildcard_packages,
                        locals_from_new=locals_from_new,
                        params_set=params_set,
                        package_name=package_name,
                    )
        # Also handle via ExplicitConstructorInvocation with "super" qualifier
        for _, eci in method_node.filter(jt.ExplicitConstructorInvocation):
            qualifier = getattr(eci, "qualifier", None) or ""
            if str(qualifier).lower() == "super":
                if super_type_name:
                    calls.add(f"{super_type_name}.{super_type_name}()")
                for arg in getattr(eci, "arguments", []) or []:
                    calls |= self._collect_invocations_in_expression(
                        arg,
                        var_types=var_types,
                        imports_types=imports_types,
                        autowired_fields=autowired_fields,
                        wildcard_packages=wildcard_packages,
                        locals_from_new=locals_from_new,
                        params_set=params_set,
                        package_name=package_name,
                    )

        # ------------------------------------------------------------------
        # Pattern: @Override Metadata
        # Record that a method overrides a parent/interface method.
        # javalang node: MethodDeclaration.annotations
        # e.g. @Override void save()
        # This pattern does not add new calls but records the override
        # relationship so downstream resolution can prefer the child's impl.
        # ------------------------------------------------------------------
        _is_override_method = False
        for ann in getattr(method_node, "annotations", []) or []:
            ann_name = getattr(ann, "name", "") or ""
            if ann_name == "Override":
                _is_override_method = True
                break
        if _is_override_method and super_type_name:
            method_nm = getattr(method_node, "name", None)
            if method_nm:
                # Emit a resolution hint: this method overrides the parent's version
                calls.add(f"{super_type_name}.{method_nm}()")
                _ast_qualified_methods.add(method_nm)

        # ------------------------------------------------------------------
        # Pattern: Lambda Block-Body Recursion
        # Walk calls made inside a block-bodied lambda.
        # javalang node: LambdaExpression.body (BlockStatement list)
        # e.g. x -> { validate(x); save(x); }
        # ------------------------------------------------------------------
        for _, lambda_expr in method_node.filter(jt.LambdaExpression):
            lambda_body = getattr(lambda_expr, "body", None)
            if lambda_body is None:
                continue
            # Block body: list of statements
            if isinstance(lambda_body, (list, tuple)):
                for stmt in lambda_body:
                    calls |= self._collect_invocations_in_expression(
                        stmt,
                        var_types=var_types,
                        imports_types=imports_types,
                        autowired_fields=autowired_fields,
                        wildcard_packages=wildcard_packages,
                        locals_from_new=locals_from_new,
                        params_set=params_set,
                        package_name=package_name,
                    )

        # ------------------------------------------------------------------
        # Pattern: Lambda Expression-Body Recursion
        # Walk calls made inside an expression-bodied lambda.
        # javalang node: LambdaExpression.body (Expression)
        # e.g. x -> process(x)
        # ------------------------------------------------------------------
        for _, lambda_expr in method_node.filter(jt.LambdaExpression):
            lambda_body = getattr(lambda_expr, "body", None)
            if lambda_body is None:
                continue
            # Expression body: a single node (not a list)
            if not isinstance(lambda_body, (list, tuple)):
                calls |= self._collect_invocations_in_expression(
                    lambda_body,
                    var_types=var_types,
                    imports_types=imports_types,
                    autowired_fields=autowired_fields,
                    wildcard_packages=wildcard_packages,
                    locals_from_new=locals_from_new,
                    params_set=params_set,
                    package_name=package_name,
                )

        # ------------------------------------------------------------------
        # Pattern: Method References  (Type::method)
        # Dedicated handling for :: — currently a blind spot.
        # javalang node: MethodReference
        # e.g. User::getName
        # ------------------------------------------------------------------
        for _, mref in method_node.filter(jt.MethodReference):
            mref_member = getattr(mref, "member", None)
            mref_qualifier = getattr(mref, "qualifier", None)
            if mref_member and mref_qualifier:
                # qualifier may be a type name string or a ReferenceType node
                if hasattr(mref_qualifier, "name"):
                    qual_str = self._simple_type_name(mref_qualifier)
                else:
                    qual_str = str(mref_qualifier) if mref_qualifier else ""
                qual_str = qual_str.strip() if qual_str else ""
                if qual_str and qual_str[0].isupper():
                    # Static or instance method ref on a class: User::getName
                    calls.add(f"{qual_str}.{mref_member}()")
                    _ast_qualified_methods.add(mref_member)
                elif qual_str:
                    # Lowercase qualifier — try to resolve via var_types
                    resolved = var_types.get(qual_str) or autowired_fields.get(qual_str) or qual_str
                    if isinstance(resolved, str) and "." in resolved and resolved[0].islower():
                        resolved = resolved.split(".")[-1]
                    if resolved and resolved[0].isupper():
                        calls.add(f"{resolved}.{mref_member}()")
                        _ast_qualified_methods.add(mref_member)

        # ------------------------------------------------------------------
        # Pattern: Constructor Method References  (Type::new)
        # Treat ::new as a constructor reference.
        # javalang node: MethodReference with member == "new"
        # e.g. Order::new
        # ------------------------------------------------------------------
        for _, mref in method_node.filter(jt.MethodReference):
            mref_member = getattr(mref, "member", None)
            mref_qualifier = getattr(mref, "qualifier", None)
            if mref_member == "new" and mref_qualifier:
                if hasattr(mref_qualifier, "name"):
                    qual_str = self._simple_type_name(mref_qualifier)
                else:
                    qual_str = str(mref_qualifier) if mref_qualifier else ""
                qual_str = (qual_str or "").strip()
                if qual_str and qual_str[0].isupper():
                    # Constructor reference: emit as Type.Type()
                    calls.add(f"{qual_str}.{qual_str}()")

        # ------------------------------------------------------------------
        # Pattern: Anonymous-Class Body Recursion
        # Walk anonymous class bodies for method calls.
        # javalang node: ClassCreator.body
        # e.g. new Runnable() { public void run(){ save(); } }
        # ------------------------------------------------------------------
        for _, cc in method_node.filter(jt.ClassCreator):
            anon_body = getattr(cc, "body", None)
            if not anon_body:
                continue
            # body is a list of member declarations; iterate methods inside
            for anon_member in anon_body:
                if isinstance(anon_member, jt.MethodDeclaration):
                    anon_stmts = getattr(anon_member, "body", None) or []
                    for stmt in anon_stmts:
                        calls |= self._collect_invocations_in_expression(
                            stmt,
                            var_types=var_types,
                            imports_types=imports_types,
                            autowired_fields=autowired_fields,
                            wildcard_packages=wildcard_packages,
                            locals_from_new=locals_from_new,
                            params_set=params_set,
                            package_name=package_name,
                        )

        # ------------------------------------------------------------------
        # Pattern: Double-Brace Initialization
        # Traverse anonymous/initializer double-brace bodies.
        # javalang node: ClassCreator.body -> member initializer blocks
        # e.g. new X() {{ init(); }}
        # ------------------------------------------------------------------
        for _, cc in method_node.filter(jt.ClassCreator):
            anon_body = getattr(cc, "body", None)
            if not anon_body:
                continue
            for anon_member in anon_body:
                # Initializer blocks inside anonymous class bodies
                member_stmts = getattr(anon_member, "statements", None) or []
                if not isinstance(anon_member, jt.MethodDeclaration) and member_stmts:
                    for stmt in member_stmts:
                        calls |= self._collect_invocations_in_expression(
                            stmt,
                            var_types=var_types,
                            imports_types=imports_types,
                            autowired_fields=autowired_fields,
                            wildcard_packages=wildcard_packages,
                            locals_from_new=locals_from_new,
                            params_set=params_set,
                            package_name=package_name,
                        )

        # ------------------------------------------------------------------
        # Pattern: Array Initializer Traversal
        # Walk object constructions inside array initializers.
        # javalang node: ArrayInitializer.initializers
        # e.g. { new A(), new B() }
        # ------------------------------------------------------------------
        for _, arr_init in method_node.filter(jt.ArrayInitializer):
            for init_item in getattr(arr_init, "initializers", []) or []:
                calls |= self._collect_invocations_in_expression(
                    init_item,
                    var_types=var_types,
                    imports_types=imports_types,
                    autowired_fields=autowired_fields,
                    wildcard_packages=wildcard_packages,
                    locals_from_new=locals_from_new,
                    params_set=params_set,
                    package_name=package_name,
                )

        # ------------------------------------------------------------------
        # Pattern: @Resource Injection
        # Expand DI annotation handling beyond @Autowired/@Inject.
        # javalang node: FieldDeclaration.annotations (@Resource)
        # e.g. @Resource Service service;
        # This pattern supplements _collect_autowired_fields which already
        # collects ALL field declarations. Here we additionally ensure that
        # fields annotated with @Resource are tracked in var_types so that
        # subsequent method calls on them are resolved correctly.
        # ------------------------------------------------------------------
        for _, fd in type_node.filter(jt.FieldDeclaration):
            _has_resource_ann = False
            for ann in getattr(fd, "annotations", []) or []:
                ann_name = getattr(ann, "name", "") or ""
                if ann_name == "Resource":
                    _has_resource_ann = True
                    break
            if _has_resource_ann:
                tname = self._effective_type_name(fd.type)
                for decl in getattr(fd, "declarators", []) or []:
                    vname = getattr(decl, "name", None)
                    if vname and tname:
                        var_types.setdefault(vname, tname)
                        autowired_fields.setdefault(vname, tname)

        # ------------------------------------------------------------------
        # Pattern: instanceof Pattern Variable
        # Capture the pattern-variable's type for subsequent call resolution.
        # javalang node: InstanceOfExpression (pattern variable — Java 16+)
        # For Java 8 this is a no-op (pattern variables not supported), but
        # the guard keeps the code safe across adapter versions.
        # e.g. if (x instanceof Service s) s.run();
        # ------------------------------------------------------------------
        _instance_of_node_name = "InstanceOfExpression"
        if hasattr(jt, _instance_of_node_name):
            _io_cls = getattr(jt, _instance_of_node_name)
            for _, io_expr in method_node.filter(_io_cls):
                pattern_var = getattr(io_expr, "pattern_variable", None)
                io_type = getattr(io_expr, "type", None)
                if pattern_var and io_type:
                    pv_name = getattr(pattern_var, "name", None)
                    pv_type = self._effective_type_name(io_type)
                    if pv_name and pv_type:
                        var_types.setdefault(pv_name, pv_type)

        # ------------------------------------------------------------------
        # Pattern: Generic Type-Witness Calls
        # Dedicated node-level handling for type-witness method invocations.
        # javalang node: MethodInvocation.type_arguments
        # e.g. repo.<User>find(id)
        # This is handled through the existing MethodInvocation loop above,
        # but here we emit an additional pass that explicitly checks for
        # non-empty type_arguments to avoid relying only on strip_generics
        # text cleanup in the chained_pat regex path.
        # ------------------------------------------------------------------
        for _, inv in method_node.filter(jt.MethodInvocation):
            type_args = getattr(inv, "type_arguments", None)
            if not type_args:
                continue
            qual = inv.qualifier or ""
            member = inv.member
            if qual and not _is_dynamic(qual):
                if qual.startswith("this."):
                    qual = qual[5:]
                qual = self._normalize_qualifier(qual, var_types)
                if self._keep_qualified_call(
                    qual, var_types, imports_types, autowired_fields,
                    wildcard_packages, locals_from_new, params_set, package_name,
                ):
                    resolved_type = var_types.get(qual) or autowired_fields.get(qual) or qual
                    if isinstance(resolved_type, str) and "." in resolved_type and resolved_type[0].islower():
                        resolved_type = resolved_type.split(".")[-1]
                    calls.add(f"{resolved_type}.{member}()")
                    _ast_qualified_methods.add(member)
            elif not qual:
                if self._keep_unqualified_call(member, set(), set()):
                    calls.add(f"{member}()")

        # ------------------------------------------------------------------
        # Pattern: Array-Indexed Receiver
        # Resolve a method call whose receiver is an array-index expression.
        # javalang node: ArraySelector / MemberReference on indexed target
        # e.g. services[i].process()
        # The existing MethodInvocation loop already captures these when the
        # qualifier contains bracket syntax as a dynamic qualifier string.
        # Here we additionally handle ArraySelector selectors on MemberReference
        # so that the method after the index dereference is emitted.
        # ------------------------------------------------------------------
        for _, inv in method_node.filter(jt.MethodInvocation):
            qual = inv.qualifier or ""
            member = inv.member
            if not qual:
                continue
            # Dynamic qualifier containing array access (brackets in the string)
            if "[" in qual or "]" in qual:
                # Strip array index expressions to get the base variable
                base_var = re.sub(r'\[.*?\]', '', qual).strip().rstrip(".")
                if base_var:
                    resolved = var_types.get(base_var) or autowired_fields.get(base_var) or base_var
                    if isinstance(resolved, str) and "." in resolved and resolved[0].islower():
                        resolved = resolved.split(".")[-1]
                    if resolved and resolved[0].isupper():
                        calls.add(f"{resolved}.{member}()")
                        _ast_qualified_methods.add(member)

        # ------------------------------------------------------------------
        # Pattern: Cast-Wrapped Receiver
        # Use the cast's target type to resolve the receiver's class.
        # javalang node: Cast (type, expression)
        # e.g. ((Service)obj).process()
        # Collect method calls on cast expressions discovered in the AST.
        # ------------------------------------------------------------------
        for _, cast_expr in method_node.filter(jt.Cast):
            cast_type = getattr(cast_expr, "type", None)
            cast_sub = getattr(cast_expr, "expression", None)
            if cast_type is None or cast_sub is None:
                continue
            cast_type_name = self._simple_type_name(cast_type)
            if not cast_type_name or not cast_type_name[0].isupper():
                continue
            # Record cast type in var_types if sub-expression is a MemberReference
            if isinstance(cast_sub, jt.MemberReference):
                var_name = getattr(cast_sub, "member", None)
                if var_name:
                    var_types.setdefault(var_name, cast_type_name)
            # Collect any nested invocations inside the cast expression
            calls |= self._collect_invocations_in_expression(
                cast_sub,
                var_types=var_types,
                imports_types=imports_types,
                autowired_fields=autowired_fields,
                wildcard_packages=wildcard_packages,
                locals_from_new=locals_from_new,
                params_set=params_set,
                package_name=package_name,
            )

        # ------------------------------------------------------------------
        # Pattern: Static/Instance Initializer Traversal
        # Treat initializer blocks as pseudo-method contexts and collect calls.
        # javalang node: ClassDeclaration.body -> initializer block members
        # e.g. static { initialize(); }
        # Note: This is a class-level pattern; we collect calls from ALL
        # initializer blocks declared in the enclosing type_node when the
        # current method_node is the constructor (common entry point).
        # ------------------------------------------------------------------
        _is_constructor = isinstance(method_node, jt.ConstructorDeclaration)
        if _is_constructor:
            for init_member in getattr(type_node, "body", []) or []:
                # javalang represents initializer blocks as BlockStatement lists
                # attached directly to the class body (not as methods/fields).
                member_stmts = None
                if hasattr(init_member, "statements") and not isinstance(
                    init_member, (jt.MethodDeclaration, jt.ConstructorDeclaration, jt.FieldDeclaration)
                ):
                    member_stmts = getattr(init_member, "statements", None)
                if member_stmts:
                    for stmt in member_stmts:
                        calls |= self._collect_invocations_in_expression(
                            stmt,
                            var_types=var_types,
                            imports_types=imports_types,
                            autowired_fields=autowired_fields,
                            wildcard_packages=wildcard_packages,
                            locals_from_new=locals_from_new,
                            params_set=params_set,
                            package_name=package_name,
                        )

        # ------------------------------------------------------------------
        # Pattern: Inheritance Resolution
        # Build parent-class relationships and use them when resolving
        # inherited method calls.
        # javalang node: ClassDeclaration.extends
        # e.g. class Child extends Parent
        # The super_type_name variable (already set above) handles existing
        # super.method() calls. Here we additionally register the parent class
        # in var_types under the keyword "super" so downstream resolution
        # can match super-rooted calls.
        # ------------------------------------------------------------------
        if super_type_name:
            var_types.setdefault("super", super_type_name)

        # ------------------------------------------------------------------
        # Pattern: Interface -> Implementation Map
        # Resolve interface-typed variables to concrete implementations.
        # javalang node: ClassDeclaration.implements
        # e.g. class Impl implements Service
        # Record interface names implemented by the current class so that
        # calls emitted with the interface type can be resolved to the impl.
        # ------------------------------------------------------------------
        _implemented_interfaces = []
        for iface_ref in getattr(type_node, "implements", []) or []:
            iface_name = self._simple_type_name(iface_ref)
            if iface_name:
                _implemented_interfaces.append(iface_name)
        # For each implemented interface, if a local var/field has that interface
        # type, add the current class as an alternative resolution target.
        _current_class_name = getattr(type_node, "name", None)
        if _current_class_name:
            for iface_name in _implemented_interfaces:
                for var_name, var_type in list(var_types.items()):
                    if var_type == iface_name:
                        # Don't overwrite; provide the impl type as a fallback
                        var_types.setdefault(var_name + "__impl__", _current_class_name)

        # ------------------------------------------------------------------
        # Pattern: Local Class Traversal
        # Walk method-scoped (local) classes and their methods.
        # javalang node: LocalClassDeclaration (if available in this javalang)
        # e.g. class Local { void run(){ save(); } }
        # ------------------------------------------------------------------
        _local_class_node_name = "LocalClassDeclaration"
        if hasattr(jt, _local_class_node_name):
            _lc_cls = getattr(jt, _local_class_node_name)
            for _, local_cls in method_node.filter(_lc_cls):
                for local_member in getattr(local_cls, "body", []) or []:
                    if isinstance(local_member, jt.MethodDeclaration):
                        local_stmts = getattr(local_member, "body", None) or []
                        for stmt in local_stmts:
                            calls |= self._collect_invocations_in_expression(
                                stmt,
                                var_types=var_types,
                                imports_types=imports_types,
                                autowired_fields=autowired_fields,
                                wildcard_packages=wildcard_packages,
                                locals_from_new=locals_from_new,
                                params_set=params_set,
                                package_name=package_name,
                            )

        # ------------------------------------------------------------------
        # Pattern: Inner-Class Construction
        # Handle inner-class instantiation via an outer object.
        # javalang node: ClassCreator (qualifier = outer instance)
        # e.g. outer.new Inner()
        # InnerClassCreator node in javalang represents "outer.new Inner()"
        # ------------------------------------------------------------------
        if hasattr(jt, "InnerClassCreator"):
            for _, icc in method_node.filter(jt.InnerClassCreator):
                inner_type = getattr(icc, "type", None)
                if inner_type is None:
                    continue
                inner_type_name = self._simple_type_name(inner_type)
                if not inner_type_name:
                    continue
                ctor_args = getattr(icc, "arguments", []) or []
                if ctor_args:
                    calls.add(f"{inner_type_name}.{inner_type_name}()")
                for arg in ctor_args:
                    calls |= self._collect_invocations_in_expression(
                        arg,
                        var_types=var_types,
                        imports_types=imports_types,
                        autowired_fields=autowired_fields,
                        wildcard_packages=wildcard_packages,
                        locals_from_new=locals_from_new,
                        params_set=params_set,
                        package_name=package_name,
                    )
        # Also detect inner-class construction encoded as ClassCreator with a
        # non-None qualifier (some javalang versions use this representation)
        for _, cc in method_node.filter(jt.ClassCreator):
            cc_qualifier = getattr(cc, "qualifier", None)
            if cc_qualifier is None:
                continue
            # A non-None qualifier means this is an outer.new Inner() form
            ctor_type = self._simple_type_name(getattr(cc, "type", None))
            if not ctor_type:
                continue
            ctor_args = getattr(cc, "arguments", []) or []
            if ctor_args:
                calls.add(f"{ctor_type}.{ctor_type}()")
            for arg in ctor_args:
                calls |= self._collect_invocations_in_expression(
                    arg,
                    var_types=var_types,
                    imports_types=imports_types,
                    autowired_fields=autowired_fields,
                    wildcard_packages=wildcard_packages,
                    locals_from_new=locals_from_new,
                    params_set=params_set,
                    package_name=package_name,
                )

        # ==============================================================
        # END OF NEW PATTERNS
        # ==============================================================

        calls = {c for c in calls if not self._is_enum_runtime_accessor_call(c)}
        return sorted(calls)

    # ------------------------------------------------------------------
    # Fallback parse (regex-only path when AST fails)
    # ------------------------------------------------------------------

    def _parse_com_imports_fallback(self, java_code: str):
        re_import_line = self._rx("import_static", flags=re.MULTILINE)
        imports_types = set()
        wildcard_packages = set()
        static_members = set()
        static_wildcard_classes = set()
        for m in re_import_line.finditer(java_code):
            line = m.group(0)
            path = m.group(1)
            parts = [p for p in path.split('.') if p]
            is_static = 'static' in line
            if is_static:
                if parts[-1] == '*':
                    if len(parts) >= 2:
                        static_wildcard_classes.add(parts[-2])
                else:
                    static_members.add(parts[-1])
                    if len(parts) >= 2:
                        imports_types.add(parts[-2])
            else:
                if parts[-1] == '*':
                    wildcard_packages.add('.'.join(parts[:-1]))
                else:
                    imports_types.add(parts[-1])
        return imports_types, wildcard_packages, static_members, static_wildcard_classes

    def _extract_balanced_args(self, text: str, start_idx: int) -> str:
        if start_idx < 0 or start_idx >= len(text) or text[start_idx] != '(':
            return ""
        depth = 0
        end = None
        for i in range(start_idx, len(text)):
            ch = text[i]
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        return text[start_idx:end + 1] if end is not None else ""

    def fallback_parse(self, code_raw: str) -> dict:
        """
        Regex-only fallback for files whose AST cannot be parsed.
        Handles all Java 8 patterns including lambdas and streams
        (they appear as regular method calls in regex terms).
        """
        java_code = html.unescape(code_raw)
        package_name = self._get_package_name(java_code)
        imports_types, wildcard_packages, static_members, static_wildcard_classes = \
            self._parse_com_imports_fallback(java_code)

        re_autowired_field  = self._rx("autowired_field", flags=re.MULTILINE)
        re_loose_decl       = self._rx("variable_declaration")
        re_var_decl         = self._rx("re_var_decl")
        re_var_new          = self._rx("re_var_new")
        re_simple_call      = self._rx("re_simple_call", flags=re.MULTILINE)
        re_member_access    = self._rx("re_member_access")
        re_unqualified_call = self._rx("re_unqualified_call")
        re_chain            = self._rx("re_chain") if self.regex.get("re_chain") else re.compile(r"$^")
        re_method_with_throw = self._rx("method_with_throw", flags=re.MULTILINE | re.DOTALL)
        re_method_name_in_sig = re.compile(r'\b([A-Za-z_]\w*)\s*\(', re.MULTILINE)

        re_class_implements      = self._rx("class_implements", flags=re.MULTILINE)
        re_class_declaration     = self._rx("class_declaration", flags=re.MULTILINE)
        re_interface_declaration = self._rx("interface_declaration", flags=re.MULTILINE)

        fallback_types = {}
        for m in re_interface_declaration.finditer(java_code):
            fallback_types[m.group(1)] = "interface"
        for m in re_class_implements.finditer(java_code):
            fallback_types.setdefault(m.group(1), "class_implements_interface")
        for m in re_class_declaration.finditer(java_code):
            fallback_types.setdefault(m.group(1), "class")

        class_or_interface_name = next(iter(fallback_types.keys()), None)
        _super_of_class = {}
        _extends_re = re.compile(r'\bclass\s+(\w+)\s+extends\s+(\w+)')
        for _m_ext in _extends_re.finditer(java_code):
            _super_of_class[_m_ext.group(1)] = _m_ext.group(2)
        parent_class_name = _super_of_class.get(class_or_interface_name)

        # --- Variable / DI types ---
        autowired_fields = {}
        for m in re_autowired_field.finditer(java_code):
            raw_type = m.group(1)
            var_name = m.group(2)
            tname = re.sub(
                r'(&amp;amp;lt;[^&amp;amp;gt]+&amp;amp;gt;|&lt;[^&gt;]+&gt;)', '', raw_type
            ).strip().split('.')[-1]
            autowired_fields[var_name] = tname

        var_types = {}
        locals_from_new = set()
        params_set = set()

        for m in re_var_decl.finditer(java_code):
            raw_type, var_name = m.group(1), m.group(2)
            # Java 8: skip if type is literally 'var' (shouldn't appear, but guard anyway)
            if raw_type.strip() == 'var':
                continue
            tname = re.sub(
                r'(&amp;amp;lt;[^&amp;amp;gt]+&amp;amp;gt;|&lt;[^&gt;]+&gt;)', '', raw_type
            ).strip().split('.')[-1]
            var_types[var_name] = tname

        for m in re_var_new.finditer(java_code):
            var_name, fq_type = m.group(1), m.group(2)
            var_types.setdefault(var_name, fq_type.split('.')[-1])
            locals_from_new.add(var_name)

        for m in re_loose_decl.finditer(java_code):
            raw_type, var_name = m.group(1), m.group(2)
            if var_name in var_types or raw_type.strip() == 'var':
                continue
            tname = re.sub(
                r'(&amp;amp;lt;[^&amp;amp;gt]+&amp;amp;gt;|&lt;[^&gt;]+&gt;)', '', raw_type
            ).strip().split('.')[-1]
            var_types[var_name] = tname

        var_types.update(autowired_fields)

        java_keywords = {"return", "this", "super", "new"} | set(
            self.details.get("control_keywords", [])
        )

        per_method_calls = []

        # Pre-build set of declared method names in this file so unqualified-call
        # lookup is O(1) instead of O(N) re.search per call per block.
        _declared_method_names: set = set()
        if class_or_interface_name:
            _decl_method_re = re.compile(
                r'\b(?:public|private|protected)\b[^{;]*\b([A-Za-z_]\w*)\s*\(',
                re.MULTILINE,
            )
            for _dm in _decl_method_re.finditer(java_code):
                _declared_method_names.add(_dm.group(1))

        def _is_dyn(q: str) -> bool:
            return isinstance(q, str) and ("(" in q or ")" in q)

        def _resolved_call_root(qual: str) -> str:
            """Resolve qualifier variable to declared class for fallback calls."""
            resolved = var_types.get(qual) or autowired_fields.get(qual) or qual
            if isinstance(resolved, str) and '.' in resolved and resolved[0].islower():
                # Keep fallback output compatible with downstream class/path enrichment.
                return resolved.split('.')[-1]
            return resolved

        def _process_block(block_text: str, method_name):
            filtered = set()

            for m in re_simple_call.finditer(block_text):
                qual, member = m.group(1), m.group(2)
                if self._is_enum_runtime_accessor(qual, member):
                    continue
                if str(qual).strip().lower() == "super":
                    if parent_class_name:
                        filtered.add(f"{parent_class_name}.{member}()")
                    else:
                        filtered.add(f"{member}()")
                    continue
                if str(qual).strip().lower() in java_keywords:
                    continue
                if _is_dyn(qual):
                    filtered.add(f"{qual}.{member}()")
                elif self._keep_qualified_call(
                    qual, var_types, imports_types, autowired_fields,
                    wildcard_packages, locals_from_new, params_set, package_name,
                ):
                    root = _resolved_call_root(qual)
                    filtered.add(f"{root}.{member}()")

            for m in re_member_access.finditer(block_text):
                qual, member = m.group(1), m.group(2)
                if str(qual).strip().lower() in java_keywords:
                    continue
                if _is_dyn(qual):
                    filtered.add(f"{member}")
                elif self._keep_qualified_call(
                    qual, var_types, imports_types, autowired_fields,
                    wildcard_packages, locals_from_new, params_set, package_name,
                ):
                    root = _resolved_call_root(qual)
                    filtered.add(f"{root}.{member}")

            for m in re_unqualified_call.finditer(block_text):
                member = m.group(1)
                if member in java_keywords:
                    continue
                if method_name and member == method_name:
                    continue
                if class_or_interface_name and member in _declared_method_names:
                    filtered.add(f"{class_or_interface_name}.{member}()")
                elif self.include_unqualified or member in static_members or static_wildcard_classes:
                    filtered.add(f"{member}()")

            for m in re_chain.finditer(block_text):
                root = m.group(1)
                if str(root).strip().lower() in java_keywords:
                    continue
                filtered.add(m.group(0))

            # throw new ...
            for tm in _re_throw_new.finditer(block_text):
                ctor_class = tm.group(1)
                args_block = self._extract_balanced_args(block_text, tm.end() - 1)
                _args_inner = args_block[1:-1] if args_block else ""
                _args_inner = re.sub(r'//.*?$|/\*.*?\*/', '', _args_inner, flags=re.MULTILINE | re.DOTALL)
                has_ctor_args = bool(_args_inner.strip())
                if has_ctor_args:
                    filtered.add(f"{ctor_class}.{ctor_class}()")
                if args_block:
                    for sm in re_simple_call.finditer(args_block):
                        q, mem = sm.group(1), sm.group(2)
                        if str(q).strip().lower() in java_keywords:
                            continue
                        if _is_dyn(q):
                            filtered.add(f"{q}.{mem}()")
                        elif self._keep_qualified_call(
                            q, var_types, imports_types, autowired_fields,
                            wildcard_packages, locals_from_new, params_set, package_name,
                        ):
                            root = _resolved_call_root(q)
                            filtered.add(f"{root}.{mem}()")
                    for um in re_unqualified_call.finditer(args_block):
                        mem = um.group(1)
                        if mem not in java_keywords and self.include_unqualified:
                            filtered.add(f"{mem}()")
                    for cm in re_chain.finditer(args_block):
                        filtered.add(cm.group(0))

            # standalone new X(...) — outside throw
            throw_pat = re.compile(r'\bthrow\s+new\s+([A-Za-z_]\w+)\s*\(', re.MULTILINE)
            new_pat = re.compile(r'\bnew\s+([A-Za-z_]\w+)\s*\(', re.MULTILINE)
            throw_positions = {tm.start() for tm in throw_pat.finditer(block_text)}
            for nm in new_pat.finditer(block_text):
                # Skip if this new is part of a throw new (already handled above)
                preceding = block_text[max(0, nm.start() - 10):nm.start()].strip()
                if preceding.endswith('throw'):
                    continue
                ctor_class = nm.group(1)
                args_block = self._extract_balanced_args(block_text, nm.end() - 1)
                _args_inner = args_block[1:-1] if args_block else ""
                _args_inner = re.sub(r'//.*?$|/\*.*?\*/', '', _args_inner, flags=re.MULTILINE | re.DOTALL)
                has_ctor_args = bool(_args_inner.strip())
                if has_ctor_args:
                    filtered.add(f"{ctor_class}.{ctor_class}()")
                if args_block:
                    for sm in re_simple_call.finditer(args_block):
                        q, mem = sm.group(1), sm.group(2)
                        if str(q).strip().lower() in java_keywords:
                            continue
                        if _is_dyn(q):
                            filtered.add(f"{q}.{mem}()")
                        elif self._keep_qualified_call(
                            q, var_types, imports_types, autowired_fields,
                            wildcard_packages, locals_from_new, params_set, package_name,
                        ):
                            root = _resolved_call_root(q)
                            filtered.add(f"{root}.{mem}()")
                    for um in re_unqualified_call.finditer(args_block):
                        mem = um.group(1)
                        if mem not in java_keywords and self.include_unqualified:
                            filtered.add(f"{mem}()")
                    for cm in re_chain.finditer(args_block):
                        filtered.add(cm.group(0))

            for dyn in self._extract_dynamic_terminal_methods(block_text):
                filtered.add(dyn)

            for call in sorted(filtered):
                if self._is_enum_runtime_accessor_call(call):
                    continue
                per_method_calls.append({'method_name': method_name, 'object_call': call})

        # Walk method bodies with a balanced signature scan.
        # This avoids greedy cross-method matches from config regexes.
        method_sig_head = re.compile(
            r'''^[ \t]*(?:@\w+(?:\([^)]*\))?\s*)*
                (?:(?:public|private|protected)\s+)?
                (?:static\s+|final\s+|synchronized\s+|native\s+|abstract\s+|default\s+)*
                (?:<[^>{}]+>\s+)?
                (?:[A-Za-z_][\w$.]*(?:\s*<[^>{}]+>)?(?:\s*\[\s*\])?\s+)+
                ([A-Za-z_]\w*)\s*\(
            ''',
            re.MULTILINE | re.VERBOSE,
        )

        def _scan_method_blocks(src_text: str):
            for mh in method_sig_head.finditer(src_text):
                method_name = mh.group(1)
                paren_open = src_text.find('(', mh.end() - 1)
                if paren_open == -1:
                    continue

                # Balance method parameter parentheses.
                pdepth = 0
                paren_close = None
                for pi in range(paren_open, len(src_text)):
                    ch = src_text[pi]
                    if ch == '(':
                        pdepth += 1
                    elif ch == ')':
                        pdepth -= 1
                        if pdepth == 0:
                            paren_close = pi
                            break
                if paren_close is None:
                    continue

                # Skip optional throws clause and whitespace to find body start.
                sig_tail = src_text[paren_close + 1:]
                m_tail = re.match(r'^\s*(?:throws\s+[^\{]+)?\s*\{', sig_tail)
                if not m_tail:
                    continue
                brace_pos = paren_close + 1 + m_tail.group(0).rfind('{')

                brace_count, end_idx = 0, None
                for i in range(brace_pos, len(src_text)):
                    if src_text[i] == '{':
                        brace_count += 1
                    elif src_text[i] == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            end_idx = i
                            break
                if end_idx is None:
                    continue

                yield src_text[mh.start():end_idx + 1], method_name

        for method_text, method_name in _scan_method_blocks(java_code):
            _process_block(method_text, method_name)

        # Constructor bodies
        if class_or_interface_name:
            ctor_pat = re.compile(
                r'(?:public|protected|private)\s+' + re.escape(class_or_interface_name) + r'\s*\([^)]*\)\s*\{',
                re.MULTILINE,
            )
            for cm in ctor_pat.finditer(java_code):
                brace_pos = java_code.find('{', cm.end() - 1)
                if brace_pos == -1:
                    continue
                brace_count, end_idx = 0, None
                for i in range(brace_pos, len(java_code)):
                    if java_code[i] == '{':
                        brace_count += 1
                    elif java_code[i] == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            end_idx = i
                            break
                if end_idx is None:
                    continue
                ctor_text = java_code[cm.start():end_idx + 1]
                _process_block(ctor_text, class_or_interface_name)

        if per_method_calls:
            row_type = fallback_types.get(class_or_interface_name or '', 'Unknown')
            return {
                'type_name': class_or_interface_name or 'Unknown',
                'row_type': row_type,
                'per_method_calls': per_method_calls,
            }

        row_type = fallback_types.get(class_or_interface_name or '', 'Unknown')
        return {
            'type_name': class_or_interface_name or 'Unknown',
            'row_type': row_type,
            'filtered_calls': [],
        }

    # ------------------------------------------------------------------
    # System-call filter
    # ------------------------------------------------------------------

    def is_system_call(self, call: str) -> bool:
        if not isinstance(call, str):
            return False
        call = call.strip()
        if not call:
            return False

        call_ng = re.sub(r'\s*&amp;amp;lt;[^&amp;amp;gt]+&amp;amp;gt;\s*', '', call)
        call_ng = re.sub(r'\s*&lt;[^&gt;]+&gt;\s*', '', call_ng)
        lc = call_ng.lower()

        system_qualifiers = self.details.get("SYSTEM_QUALIFIERS", [
            r"^logger\.", r"^log\.", r"^system\.", r"^string\.", r"^objects\.", r"^arrays\.",
            r"^collections\.", r"^optional\.", r"^stream\.", r"^httpsecurity\.", r"^security\.",
        ])
        for pattern in system_qualifiers:
            if re.match(pattern, lc):
                return True

        def extract_method(c: str) -> str:
            part = c.split(".")[-1]
            part = re.sub(r"\(.*\)", "", part)
            return part.replace(";", "").replace('"', "").replace("'", "").strip().lower()

        default_system_methods = {"equals"}
        system_methods = default_system_methods | {
            m.lower() for m in self.details.get("SYSTEM_METHODS", [])
        }
        return extract_method(call_ng) in system_methods

    def language_keywords(self) -> set:
        return {"return", "this", "super", "new"} | set(self.details.get("control_keywords", []))

    # ------------------------------------------------------------------
    # Object-class map
    # ------------------------------------------------------------------

    def build_object_class_map(self, app_folder: str) -> dict:
        obj_class_map = {}
        PRIMITIVES = set(self.details.get("PRIMITIVE", []))
        COLLECTION_TYPES = set(self.details.get("COLLECTION_TYPES", [
            "List", "Set", "Map", "Collection", "Iterable"
        ]))

        var_decl_pattern  = self._rx("var_decl_pattern", flags=re.MULTILINE)
        for_loop_pattern  = self._rx("for_loop_pattern", flags=re.MULTILINE)
        simple_local_decl = re.compile(r'\b([A-Za-z_]\w*)\s+([A-Za-z_]\w*)\s*=')
        method_param_list_pat = re.compile(
            r'\b(?:public|protected|private)\b[^{;]*\(([^)]*)\)', re.MULTILINE
        )
        method_param_decl_pat = self._rx("method_param_decl")

        def clean_type(t: str) -> str:
            if not t:
                return t
            t2 = re.sub(r'\s*&amp;amp;lt;[^&amp;amp;gt]+&amp;amp;gt;\s*', '', t)
            t2 = re.sub(r'\s*&lt;[^&gt;]+&gt;\s*', '', t2)
            return t2.replace('[]', '').strip()

        def update_map(f: str, var: str, typ: str, *, source: str):
            if not typ or typ in PRIMITIVES:
                return
            # FIX: the lookup side in _enrich_call_with_path uses
            #   object_class_map.get((caller_file.lower(), cls_name.lower()))
            # where caller_file is the full absolute path stored in the file_name
            # column (set from fpath = os.path.join(root, file) in the parser worker).
            # Previously this side used f.lower() where f was the bare filename
            # ('OrderService.java'), so the scoped key never matched the lookup key
            # (which was the full path).  Use os.path.normcase(os.path.abspath(f))
            # to make both sides consistent regardless of relative/absolute form.
            key_scoped = (os.path.normcase(os.path.abspath(f)), var.lower())
            key_global = var.lower()
            existing_s = obj_class_map.get(key_scoped)
            if existing_s:
                if existing_s in COLLECTION_TYPES and typ not in COLLECTION_TYPES:
                    obj_class_map[key_scoped] = typ
            else:
                obj_class_map[key_scoped] = typ
            existing_g = obj_class_map.get(key_global)
            if existing_g:
                if existing_g in COLLECTION_TYPES and typ not in COLLECTION_TYPES:
                    obj_class_map[key_global] = typ
            else:
                obj_class_map[key_global] = typ

        for root, _, files in os.walk(app_folder):
            for file in files:
                if not file.endswith(self.file_extension()):
                    continue
                fpath = os.path.join(root, file)
                # FIX 3: reuse cached file content and parsed AST
                try:
                    if fpath in self._file_content_cache:
                        code = self._file_content_cache[fpath]
                    else:
                        with open(fpath, "r", encoding="utf-8") as fh:
                            code = fh.read()
                        self._file_content_cache[fpath] = code
                except Exception:
                    continue

                # --- AST path ---
                _ast_key = fpath
                try:
                    if _ast_key in self._raw_ast_cache:
                        parsed = self._raw_ast_cache[_ast_key]
                        if parsed is False:
                            raise Exception("cached parse failure")
                    else:
                        parsed = javalang.parse.parse(code)
                        self._raw_ast_cache[_ast_key] = parsed
                    for _, type_node in parsed.filter(jt.ClassDeclaration):
                        for _, fd in type_node.filter(jt.FieldDeclaration):
                            tname = self._effective_type_name(fd.type)
                            for decl in getattr(fd, "declarators", []):
                                if tname and tname not in PRIMITIVES:
                                    update_map(fpath, decl.name, tname, source="ast_field")

                        for _, mnode in type_node.filter(jt.MethodDeclaration):
                            for p in getattr(mnode, "parameters", []):
                                tname = self._effective_type_name(p.type)
                                if tname and tname not in PRIMITIVES:
                                    update_map(fpath, p.name, tname, source="ast_param")
                            for _, lv in mnode.filter(jt.LocalVariableDeclaration):
                                declared_type = self._effective_type_name(lv.type)
                                for decl in getattr(lv, "declarators", []):
                                    tname = declared_type or self._infer_type_from_initializer(decl)
                                    if tname and tname not in PRIMITIVES:
                                        update_map(fpath, decl.name, tname, source="ast_local")
                            for _, forstmt in mnode.filter(jt.ForStatement):
                                if hasattr(forstmt, "control") and hasattr(forstmt.control, "var"):
                                    var_decl = forstmt.control.var
                                    if var_decl:
                                        tname = self._effective_type_name(var_decl.type)
                                        for declarator in getattr(var_decl, "declarators", []):
                                            if tname and tname not in PRIMITIVES:
                                                update_map(fpath, declarator.name, tname, source="ast_for")

                        for _, cnode in type_node.filter(jt.ConstructorDeclaration):
                            for p in getattr(cnode, "parameters", []):
                                tname = self._effective_type_name(p.type)
                                if tname and tname not in PRIMITIVES:
                                    update_map(fpath, p.name, tname, source="ast_ctor_param")
                            for _, lv in cnode.filter(jt.LocalVariableDeclaration):
                                declared_type = self._effective_type_name(lv.type)
                                for decl in getattr(lv, "declarators", []):
                                    tname = declared_type or self._infer_type_from_initializer(decl)
                                    if tname and tname not in PRIMITIVES:
                                        update_map(fpath, decl.name, tname, source="ast_ctor_local")
                    continue
                except Exception:
                    pass

                # --- Regex fallback ---
                for m in var_decl_pattern.finditer(code):
                    raw_type, var_name = m.group(1), m.group(2)
                    t = clean_type(raw_type)
                    if t and t not in PRIMITIVES:
                        update_map(file, var_name, t, source="regex_var_decl")

                for m in for_loop_pattern.finditer(code):
                    raw_type, var_name = m.group(1), m.group(2)
                    t = clean_type(raw_type)
                    if t and t not in PRIMITIVES:
                        update_map(file, var_name, t, source="regex_for_loop")

                for m in simple_local_decl.finditer(code):
                    raw_type, var_name = m.group(1), m.group(2)
                    t = clean_type(raw_type)
                    if t and t not in PRIMITIVES:
                        update_map(file, var_name, t, source="regex_simple_local")

                for pl_match in method_param_list_pat.finditer(code):
                    for pm in method_param_decl_pat.finditer(pl_match.group(1)):
                        raw_type, var_name = pm.group(1), pm.group(2)
                        t = clean_type(raw_type)
                        if t and t not in PRIMITIVES:
                            update_map(file, var_name, t, source="regex_param")

        return obj_class_map

    # ------------------------------------------------------------------
    # Method return index
    # ------------------------------------------------------------------

    def build_method_return_index(self, app_folder: str) -> dict:
        method_return_index = {}
        class_decl_pat = re.compile(r'\bclass\s+(\w+)\b')
        # Captures "class Foo extends Bar" — used for Case 1 inheritance walk
        class_extends_pat = re.compile(r'\bclass\s+(\w+)\s+extends\s+(\w+)')
        method_sig_pat = re.compile(
            r'(?:public|protected|private)?\s+(?:static\s+)?([\w\.&lt;<>\[\]]+)\s+(\w+)\s*\(',
            re.MULTILINE,
        )
        constructor_sig_pat = re.compile(
            r'(?:public|protected|private)\s+(\w+)\s*\(', re.MULTILINE
        )

        for root, _, files in os.walk(app_folder):
            for file in files:
                if not file.endswith(self.file_extension()):
                    continue
                fpath = os.path.join(root, file)
                # FIX 3: reuse cached file content and parsed AST
                try:
                    if fpath in self._file_content_cache:
                        code = self._file_content_cache[fpath]
                    else:
                        with open(fpath, "r", encoding="utf-8") as f:
                            code = f.read()
                        self._file_content_cache[fpath] = code
                except Exception:
                    continue

                _ast_key2 = fpath
                if _ast_key2 in self._raw_ast_cache:
                    _cached2 = self._raw_ast_cache[_ast_key2]
                    parsed = None if (_cached2 is False) else _cached2
                else:
                    try:
                        parsed = javalang.parse.parse(code)
                        self._raw_ast_cache[_ast_key2] = parsed
                    except Exception:
                        parsed = None
                        self._raw_ast_cache[_ast_key2] = False

                if parsed:
                    for _, cls in parsed.filter(jt.ClassDeclaration):
                        cls_name = getattr(cls, "name", None)
                        if not cls_name:
                            continue
                        method_return_index.setdefault(cls_name, {})
                        # Case 1: record parent class so service can walk extends chain
                        parent_type = getattr(cls, "extends", None)
                        if parent_type is not None:
                            parent_name = getattr(parent_type, "name", None)
                            if parent_name:
                                method_return_index[cls_name]["__extends__"] = parent_name
                        for _, m in cls.filter(jt.MethodDeclaration):
                            rt = m.return_type
                            if rt is None:
                                rname = "void"
                            else:
                                base = rt.name if hasattr(rt, "name") else "Unknown"
                                rname = re.sub(
                                    r'(&amp;lt;[^&amp;gt]+&amp;gt;|&lt;[^&gt;]+&gt;)', '', base
                                )
                            method_return_index[cls_name][m.name] = rname.strip().split('.')[-1]
                        for _, c in cls.filter(jt.ConstructorDeclaration):
                            method_return_index[cls_name][c.name] = "<constructor>"

                    for _, itf in parsed.filter(jt.InterfaceDeclaration):
                        itf_name = getattr(itf, "name", None)
                        if not itf_name:
                            continue
                        method_return_index.setdefault(itf_name, {})
                        for _, m in itf.filter(jt.MethodDeclaration):
                            rt = m.return_type
                            if rt is None:
                                rname = "void"
                            else:
                                base = rt.name if hasattr(rt, "name") else "Unknown"
                                rname = re.sub(
                                    r'(&amp;lt;[^&amp;gt]+&amp;gt;|&lt;[^&gt;]+&gt;)', '', base
                                )
                            method_return_index[itf_name][m.name] = rname.strip().split('.')[-1]
                    continue

                # Regex fallback
                cls_match = class_decl_pat.search(code)
                if not cls_match:
                    continue
                cls_name = cls_match.group(1)
                method_return_index.setdefault(cls_name, {})
                # Case 1: capture extends from regex fallback too
                ext_match = class_extends_pat.search(code)
                if ext_match and ext_match.group(1) == cls_name:
                    method_return_index[cls_name]["__extends__"] = ext_match.group(2)
                for mm in method_sig_pat.finditer(code):
                    return_type = mm.group(1)
                    method_name = mm.group(2)
                    simple_return = re.sub(
                        r'(&amp;lt;[^&amp;gt]+&amp;gt;|&lt;[^&gt;]+&gt;)', '', return_type
                    ).strip().split('.')[-1]
                    method_return_index[cls_name][method_name] = simple_return
                for cm in constructor_sig_pat.finditer(code):
                    ctor_name = cm.group(1)
                    if ctor_name == cls_name:
                        method_return_index[cls_name][ctor_name] = "<constructor>"

        return method_return_index

    # ------------------------------------------------------------------
    # File → type map
    # ------------------------------------------------------------------

    def find_type_to_file_map(self, app_folder: str) -> dict:
        java_files_map = {}
        for root, _, files in os.walk(app_folder):
            for f in files:
                if f.endswith(self.file_extension()):
                    class_name = os.path.splitext(f)[0]
                    java_files_map[class_name] = os.path.join(root, f)
        return java_files_map

    # ------------------------------------------------------------------
    # LOC counter
    # ------------------------------------------------------------------

    def extract_method_loc(
        self,
        java_file_path: str,
        method_name: str,
        classname=None,
        include_package_private: bool = False,
        count_empty_lines: bool = True,
    ):
        if not java_file_path:
            return None

        try:
            with open(java_file_path, "r", encoding="utf-8") as f:
                code = f.read()
        except Exception:
            try:
                with open(java_file_path, "r", encoding="latin-1") as f:
                    code = f.read()
            except Exception:
                return None

        code = code.replace("\r\n", "\n").replace("\r", "\n")
        lines = code.split("\n")

        access_req = r"(?:public|private|protected)"
        access = rf"(?:{access_req})?" if include_package_private else access_req
        mname_esc = re.escape(method_name)

        method_decl_pat = rf"""
            (?m)
            ^[ \t]*
            {access}[ \t]*
            (?:(?:static|final|abstract|synchronized|native|strictfp)\b[ \t]*)*
            [\w.<>\[\],? \t]+
            \b(?P<mname>{mname_esc})[ \t]*\(
        """

        constructor_decl_pat = None
        if classname and method_name == classname:
            cname_esc = re.escape(classname)
            constructor_decl_pat = rf"""
                (?m)
                ^[ \t]*
                {access}[ \t]*
                (?:(?:static|final|abstract|synchronized|native|strictfp)\b[ \t]*)*
                \b(?P<mname>{cname_esc})[ \t]*\(
            """

        patterns = []
        if constructor_decl_pat:
            patterns.append(re.compile(constructor_decl_pat, re.IGNORECASE | re.VERBOSE))
        patterns.append(re.compile(method_decl_pat, re.IGNORECASE | re.VERBOSE))

        decl_match = None
        for pat in patterns:
            decl_match = pat.search(code)
            if decl_match:
                break
        if not decl_match:
            return None

        sig_line_idx = code.count("\n", 0, decl_match.start("mname")) + 1

        def find_annotation_block_start(sig_idx):
            i = sig_idx - 2
            if i < 0:
                return None
            paren_balance = 0
            started = False
            start_line = None
            while i >= 0:
                raw = lines[i].rstrip()
                if not raw.strip() and not (started and paren_balance > 0):
                    break
                is_anno = bool(re.match(r'^[ \t]*@', raw))
                if not started:
                    if is_anno:
                        started = True
                        start_line = i + 1
                        paren_balance = raw.count("(") - raw.count(")")
                    else:
                        break
                else:
                    if is_anno or paren_balance > 0:
                        start_line = i + 1
                        paren_balance += raw.count("(") - raw.count(")")
                    else:
                        break
                i -= 1
            return start_line

        anno_start = find_annotation_block_start(sig_line_idx)
        start_line_idx = anno_start if anno_start is not None else sig_line_idx

        def find_opening_brace_line(from_line):
            in_block_comment = False
            for i in range(from_line - 1, len(lines)):
                line = lines[i]
                j, n = 0, len(line)
                in_string = False
                string_char = None
                while j < n:
                    ch = line[j]
                    nxt = line[j + 1] if j + 1 < n else ""
                    if in_block_comment:
                        if ch == "*" and nxt == "/":
                            in_block_comment = False
                            j += 2
                            continue
                        j += 1
                        continue
                    if in_string:
                        if ch == "\\":
                            j += 2
                            continue
                        if ch == string_char:
                            in_string = False
                            string_char = None
                        j += 1
                        continue
                    if ch == "/" and nxt == "*":
                        in_block_comment = True
                        j += 2
                        continue
                    if ch == "/" and nxt == "/":
                        break
                    if ch in ("'", '"'):
                        in_string = True
                        string_char = ch
                        j += 1
                        continue
                    if ch == "{":
                        return i + 1
                    j += 1
            return None

        def find_closing_brace_line(open_line):
            in_block_comment = False
            depth = 0
            started = False
            for i in range(open_line - 1, len(lines)):
                line = lines[i]
                j, n = 0, len(line)
                in_string = False
                string_char = None
                while j < n:
                    ch = line[j]
                    nxt = line[j + 1] if j + 1 < n else ""
                    if in_block_comment:
                        if ch == "*" and nxt == "/":
                            in_block_comment = False
                            j += 2
                            continue
                        j += 1
                        continue
                    if in_string:
                        if ch == "\\":
                            j += 2
                            continue
                        if ch == string_char:
                            in_string = False
                            string_char = None
                        j += 1
                        continue
                    if ch == "/" and nxt == "*":
                        in_block_comment = True
                        j += 2
                        continue
                    if ch == "/" and nxt == "/":
                        break
                    if ch in ("'", '"'):
                        in_string = True
                        string_char = ch
                        j += 1
                        continue
                    if ch == "{":
                        depth += 1
                        started = True
                    elif ch == "}":
                        depth -= 1
                        if started and depth == 0:
                            return i + 1
                    j += 1
            return None

        brace_open_line = find_opening_brace_line(sig_line_idx)
        if brace_open_line is None:
            return 1

        end_line_idx = find_closing_brace_line(brace_open_line)
        if end_line_idx is None:
            end_line_idx = len(lines)

        if count_empty_lines:
            return max(1, end_line_idx - start_line_idx + 1)
        else:
            segment = lines[start_line_idx - 1:end_line_idx]
            return max(1, sum(1 for ln in segment if ln.strip()))

    # ------------------------------------------------------------------
    # Properties extraction
    # ------------------------------------------------------------------

    def load_all_properties(self, app_folder, additional_property_refs=None):
        app_folder = Path(app_folder)
        paths = set()
        for p in app_folder.rglob("*.properties"):
            paths.add(p.resolve())

        if additional_property_refs:
            for ref in additional_property_refs:
                ref_norm = self._normalize_ps_ref(ref)
                matches = list(app_folder.rglob(ref_norm))
                if not matches:
                    matches = list(app_folder.rglob(os.path.basename(ref_norm)))
                for m in matches:
                    paths.add(m.resolve())

        props = {}
        for p in sorted(paths, key=str):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    for raw in fh:
                        line = raw.strip()
                        if not line or line.startswith("#") or line.startswith("!"):
                            continue
                        if "=" in line:
                            k, v = line.split("=", 1)
                        elif ":" in line:
                            k, v = line.split(":", 1)
                        else:
                            continue
                        props[k.strip()] = v.strip()
            except Exception as e:
                print(f"Error reading {p}: {e}")
        return props

    def extract_application_properties_from_folder(
        self,
        app_folder,
        include_filepath: bool = True,
        include_trailing_dot: bool = True,
    ):
        def _compose(jpath: Path, method_name) -> str:
            base = jpath.stem
            if method_name:
                return f"{base}.{method_name}"
            return f"{base}." if include_trailing_dot else base

        app_folder = Path(app_folder)
        ps_refs = set()
        java_paths = []

        for p in app_folder.rglob("*.java"):
            java_paths.append(p.resolve())
            try:
                txt = p.read_text(encoding="utf-8")
            except Exception:
                try:
                    txt = p.read_text(encoding="latin-1")
                except Exception:
                    txt = ""
            for m in re_property_source.finditer(txt):
                ps_refs.add(m.group(1).strip())

        properties_map = self.load_all_properties(app_folder, additional_property_refs=ps_refs)

        debug_named_query = "RequestorConfig.findByRequestorIdAndSubProcessAndKey"

        # Collect all @NamedQuery definitions first so usage sites can be resolved
        # to their JPQL text during createNamedQuery(...) extraction.
        named_query_map = {}
        for jf in java_paths:
            try:
                nq_code = jf.read_text(encoding="utf-8")
            except Exception:
                try:
                    nq_code = jf.read_text(encoding="latin-1")
                except Exception:
                    nq_code = ""
            for nm in re_named_query_decl.finditer(nq_code):
                qname = (nm.group(1) or "").strip()
                qtext = (nm.group(2) or "").strip()
                if qname and qname not in named_query_map:
                    named_query_map[qname] = qtext
                    if qname == debug_named_query:
                        print(
                            "[DEBUG][NAMED_QUERY][DECL_FOUND] "
                            f"query={qname} file={jf} value={qtext}"
                        )

        rows = []
        named_query_seen = set()

        for jf in java_paths:
            try:
                code = jf.read_text(encoding="utf-8")
            except Exception:
                try:
                    code = jf.read_text(encoding="latin-1")
                except Exception:
                    code = ""

            method_index_map = self._build_method_index_map(code)

            # @Value
            for item in self._extract_values_with_vars(code):
                key = item["Property"]
                var = item["Variable"]
                actual = properties_map.get(key, "NOT_FOUND")
                method_name = None
                if var:
                    pattern = re.compile(r'\b' + re.escape(var) + r'\b')
                    for mu in pattern.finditer(code, item["span_end"]):
                        method_name = self._find_enclosing_method(method_index_map, mu.start())
                        if method_name:
                            break
                rows.append({
                    "FileName": jf.name.replace(".java", ""),
                    "FilePath": str(jf),
                    "Filename.methodname": _compose(jf, method_name),
                    "Annotation": item["Annotation"],
                    "Property": key,
                    "Variable": var,
                    "method_name": method_name,
                    "Actual Value": actual,
                })

            # @ConfigurationProperties
            for m in re_configuration_properties.finditer(code):
                prefix = m.group(1)
                matched = {k: v for k, v in properties_map.items()
                           if k == prefix or k.startswith(prefix + ".")}
                actual = "; ".join(f"{k}={v}" for k, v in matched.items()) if matched else "NOT_FOUND"
                rows.append({
                    "FileName": jf.name.replace(".java", ""),
                    "FilePath": str(jf),
                    "Filename.methodname": _compose(jf, None),
                    "Annotation": "@ConfigurationProperties",
                    "Property": prefix,
                    "Variable": None,
                    "method_name": None,
                    "Actual Value": actual,
                })

            # @PropertySource
            for m in re_property_source.finditer(code):
                rows.append({
                    "FileName": jf.name.replace(".java", ""),
                    "FilePath": str(jf),
                    "Filename.methodname": _compose(jf, None),
                    "Annotation": "@PropertySource",
                    "Property": m.group(1),
                    "Variable": None,
                    "method_name": None,
                    "Actual Value": "FILE_REFERENCE",
                })

            # messageSource.getMessage(...)
            for mm in re_message_key.finditer(code):
                key = mm.group(1)
                actual = properties_map.get(key, "NOT_FOUND")
                method_name = self._find_enclosing_method(method_index_map, mm.start())
                rows.append({
                    "FileName": jf.name.replace(".java", ""),
                    "FilePath": str(jf),
                    "Filename.methodname": _compose(jf, method_name),
                    "Annotation": "MessageSource",
                    "Property": key,
                    "Variable": None,
                    "method_name": method_name,
                    "Actual Value": actual,
                })

            # Generic method("NamedQuery.Name", ...)
            # Do not hardcode a specific API method name; include any call where
            # the first string argument matches a declared @NamedQuery name.
            for nm in re_any_method_first_string_arg.finditer(code):
                method_token = (nm.group(1) or "").strip()
                query_name = (nm.group(2) or "").strip()
                if not query_name or query_name not in named_query_map:
                    continue
                method_name = self._find_enclosing_method(method_index_map, nm.start())
                dedup_key = (str(jf), method_name or "", method_token, query_name)
                if dedup_key in named_query_seen:
                    continue
                named_query_seen.add(dedup_key)
                rows.append({
                    "FileName": jf.name.replace(".java", ""),
                    "FilePath": str(jf),
                    "Filename.methodname": _compose(jf, method_name),
                    "Annotation": "@NamedQuery",
                    "Property": query_name,
                    "Variable": method_token or None,
                    "method_name": method_name,
                    "Actual Value": named_query_map.get(query_name, "NOT_FOUND"),
                })
                if query_name == debug_named_query:
                    print(
                        "[DEBUG][NAMED_QUERY][ROW_ADDED] "
                        f"query={query_name} caller_file={jf} "
                        f"caller_method={method_name} method_token={method_token} "
                        f"value={named_query_map.get(query_name, 'NOT_FOUND')}"
                    )

        df = pd.DataFrame(rows)
        if include_filepath:
            cols = ["FileName", "FilePath", "Filename.methodname", "Annotation",
                    "Property", "Variable", "method_name", "Actual Value"]
        else:
            cols = ["FileName", "Filename.methodname", "Annotation",
                    "Property", "Variable", "method_name", "Actual Value"]
        df = df.reindex(columns=cols)
        if "method_name" in df.columns:
            df = df[df["method_name"].notna()]
        return df

    # ------------------------------------------------------------------
    # Inline Java variable extraction (no separate .properties file)
    # ------------------------------------------------------------------

    # Matches inline assignments like:
    #   private String url = "jdbc:postgresql://localhost/db";
    #   static final int TIMEOUT = 30;
    #   private static final String Q = "a" + "b" + "c";
    # Captures:
    #   group 1 -> variable name
    #   group 2 -> full right-hand expression (single or concatenated literals)
    _re_inline_var_assign = re.compile(
        r'^[ \t]*'
        r'(?:(?:public|private|protected)\s+)?'
        r'(?:(?:static|final|transient|volatile)\s+)*'
        r'[\w<>\[\]]+\s+'
        r'([A-Za-z_]\w*)'
        r'\s*=\s*'
        r'('
        r'(?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\b\d+(?:\.\d+)?\b|\btrue\b|\bfalse\b)'
        r'(?:\s*\+\s*(?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\b\d+(?:\.\d+)?\b|\btrue\b|\bfalse\b))*'
        r')\s*;',
        re.MULTILINE | re.DOTALL,
    )

    _re_inline_value_token = re.compile(
        r'"((?:\\.|[^"\\])*)"|\'((?:\\.|[^\'\\])*)\'|(\b\d+(?:\.\d+)?\b)',
        re.DOTALL,
    )

    def _collapse_inline_assigned_value(self, rhs_expr: str) -> str:
        """Collapse concatenated Java literal expressions into one value string.

        Example:
            "a" + "b" + "c" -> "abc"
        If the expression includes unsupported syntax, returns rhs_expr as-is.
        """
        if not isinstance(rhs_expr, str):
            return ""
        expr = rhs_expr.strip()
        if not expr:
            return ""

        pieces = []
        pos = 0
        for m in self._re_inline_value_token.finditer(expr):
            gap = expr[pos:m.start()]
            if not re.fullmatch(r'(?:\s*\+\s*)*', gap):
                return expr
            pieces.append(m.group(1) or m.group(2) or m.group(3) or "")
            pos = m.end()

        tail = expr[pos:]
        if not re.fullmatch(r'(?:\s*\+\s*)*', tail):
            return expr

        return "".join(pieces) if pieces else expr

    def _literal_token_to_value(self, token: str) -> str:
        """Normalize a Java literal token to a plain value string."""
        if not isinstance(token, str):
            return ""
        t = token.strip()
        if len(t) >= 2 and ((t[0] == '"' and t[-1] == '"') or (t[0] == "'" and t[-1] == "'")):
            return t[1:-1]
        return t

    def _initializer_to_inline_value(self, init_node, known_constants=None):
        """Extract value from a field initializer.

        Supports:
          - literals
          - literal concatenation with '+'
          - same-class constant references used inside concatenation
        """
        if init_node is None:
            return None

        if known_constants is None:
            known_constants = {}

        if isinstance(init_node, jt.Literal):
            return self._literal_token_to_value(getattr(init_node, "value", ""))

        if isinstance(init_node, jt.MemberReference):
            ref_name = getattr(init_node, "member", None)
            if isinstance(ref_name, str) and ref_name in known_constants:
                return known_constants.get(ref_name)
            return None

        if isinstance(init_node, jt.BinaryOperation):
            op = getattr(init_node, "operator", None)
            if op != "+":
                return None
            left_node = getattr(init_node, "operandl", None)
            right_node = getattr(init_node, "operandr", None)
            if left_node is None:
                left_node = getattr(init_node, "left", None)
            if right_node is None:
                right_node = getattr(init_node, "right", None)
            left_val = self._initializer_to_inline_value(left_node, known_constants=known_constants)
            right_val = self._initializer_to_inline_value(right_node, known_constants=known_constants)
            if left_val is None or right_val is None:
                return None
            return f"{left_val}{right_val}"

        return None

    def _extract_inline_field_assignments_ast(self, code: str) -> dict:
        """Extract class-level assignments, including static-block assignments."""
        out = {}
        if not isinstance(code, str) or not code.strip():
            return out

        tree = self.parse_ast(code)
        if tree is None:
            return out

        field_names = set()
        try:
            # Multi-pass resolution so later constants can reference earlier ones,
            # and composite SQL constants can include other constant tokens.
            pending = []
            for _, field_decl in tree.filter(jt.FieldDeclaration):
                for decl in getattr(field_decl, "declarators", []) or []:
                    field_names.add(str(getattr(decl, "name", "") or ""))
                    init = getattr(decl, "initializer", None)
                    if init is None:
                        continue
                    pending.append((decl.name, init))

            progress = True
            while progress and pending:
                progress = False
                still_pending = []
                for name, init in pending:
                    value = self._initializer_to_inline_value(init, known_constants=out)
                    if value is None:
                        still_pending.append((name, init))
                        continue
                    out[name] = str(value).strip()
                    progress = True
                pending = still_pending
        except Exception:
            return {}

        # Capture static-block assignments to class fields such as:
        #   static { TARGET = Collections.unmodifiableMap(localMap); }
        # and resolve map-builder local values from repeated localMap.put(k, v).
        try:
            static_blocks = []
            scan_pos = 0
            code_len = len(code)
            while True:
                m_static = re.search(r'\bstatic\s*\{', code[scan_pos:])
                if not m_static:
                    break
                block_open = scan_pos + m_static.end() - 1
                depth = 0
                block_end = None
                for i in range(block_open, code_len):
                    ch = code[i]
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            block_end = i
                            break
                if block_end is None:
                    break
                static_blocks.append(code[block_open + 1:block_end])
                scan_pos = block_end + 1

            def _to_display_value(token: str) -> str:
                t = str(token or "").strip()
                if not t:
                    return ""
                if (len(t) >= 2 and t[0] == '"' and t[-1] == '"') or (len(t) >= 2 and t[0] == "'" and t[-1] == "'"):
                    return t[1:-1]
                return out.get(t, t)

            for block in static_blocks:
                local_maps = {}

                map_decl_re = re.compile(
                    r'\b(?:final\s+)?(?:Map|HashMap|LinkedHashMap|TreeMap)\s*<[^>]*>\s*([A-Za-z_]\w*)\s*=\s*new\s+[A-Za-z_]\w*\s*<[^>]*>\s*\(\s*\)\s*;',
                    re.MULTILINE,
                )
                for mm in map_decl_re.finditer(block):
                    local_maps.setdefault(mm.group(1), [])

                put_re = re.compile(
                    r'\b([A-Za-z_]\w*)\s*\.\s*put\s*\(\s*("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^,]+?)\s*,\s*("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\)]+?)\s*\)\s*;',
                    re.MULTILINE,
                )
                for pm in put_re.finditer(block):
                    map_name = pm.group(1)
                    if map_name not in local_maps:
                        continue
                    k = _to_display_value(pm.group(2))
                    v = _to_display_value(pm.group(3))
                    local_maps[map_name].append((k, v))

                local_map_values = {
                    name: "{" + ", ".join(['{}={}'.format(k, v) for k, v in pairs]) + "}"
                    for name, pairs in local_maps.items()
                    if pairs
                }

                assign_re = re.compile(r'\b([A-Za-z_]\w*)\s*=\s*([^;]+);', re.MULTILINE)
                for am in assign_re.finditer(block):
                    lhs = am.group(1)
                    rhs = (am.group(2) or "").strip()
                    if lhs not in field_names or not rhs:
                        continue
                    final_rhs = rhs
                    for map_name, map_value in local_map_values.items():
                        final_rhs = re.sub(r'\b' + re.escape(map_name) + r'\b', map_value, final_rhs)
                    out[lhs] = final_rhs
        except Exception:
            pass

        return out

    def _extract_method_body(self, java_text: str, method_name: str) -> str:
        """
        Return the source text of the first method whose name matches
        *method_name* found in *java_text*.  Returns an empty string when
        the method cannot be located.

        Strategy:
          1. Find the method declaration position via _build_method_index_map.
          2. Walk forward from that position counting braces to find the
             matching closing brace — that slice is the method body.
        """
        method_index_map = self._build_method_index_map(java_text)
        start_pos = None
        for pos, name in method_index_map:
            if name == method_name:
                start_pos = pos
                break
        if start_pos is None:
            return ""

        # Advance to the first opening brace of the method body
        brace_start = java_text.find("{", start_pos)
        if brace_start == -1:
            return ""

        depth = 0
        i = brace_start
        n = len(java_text)
        while i < n:
            ch = java_text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return java_text[brace_start: i + 1]
            i += 1
        # Unclosed brace — return whatever we have
        return java_text[brace_start:]

    def _extract_method_body_by_signature(self, java_text: str, method_name: str, parameter_types_hint: str = None) -> str:
        """Return method/constructor body using AST + optional parameter-type hint.

        The hint uses the same semicolon-separated format as Parameter_Types
        in lineage rows. Matching is best-effort and overload-aware.
        """
        if not isinstance(java_text, str) or not java_text.strip() or not method_name:
            return ""

        tree = self.parse_ast(java_text)
        if tree is None:
            return self._extract_method_body(java_text, method_name)

        def _canon(tname: str) -> str:
            if not isinstance(tname, str):
                return ""
            t = tname.strip()
            if not t:
                return ""
            t = re.sub(r'\s*<[^>]+>\s*', '', t)
            t = t.replace("...", "[]")
            return t.lower()

        def _node_param_types(params):
            out = []
            for p in params or []:
                ptype = getattr(p, "type", None)
                t = self._effective_type_name(ptype) or ""
                t += "[]" * int(getattr(ptype, "dimensions", 0) or 0)
                if getattr(p, "varargs", False):
                    t += "[]"
                out.append(_canon(t))
            return tuple(out)

        hint_tuple = tuple(
            _canon(x)
            for x in str(parameter_types_hint or "").split(";")
            if str(x).strip()
        )

        candidates = []

        for _, m in tree.filter(jt.MethodDeclaration):
            if getattr(m, "name", None) == method_name:
                candidates.append((m, _node_param_types(getattr(m, "parameters", []))))

        for _, c in tree.filter(jt.ConstructorDeclaration):
            if getattr(c, "name", None) == method_name:
                candidates.append((c, _node_param_types(getattr(c, "parameters", []))))

        if not candidates:
            return self._extract_method_body(java_text, method_name)

        chosen = None
        if hint_tuple:
            for node, ptypes in candidates:
                if ptypes == hint_tuple:
                    chosen = node
                    break
            if chosen is None:
                for node, ptypes in candidates:
                    if len(ptypes) == len(hint_tuple):
                        chosen = node
                        break

        if chosen is None:
            chosen = candidates[0][0]

        try:
            pos = getattr(chosen, "position", None)
            if pos and pos[0]:
                lines = java_text.splitlines(True)
                offsets = self._get_line_offsets(java_text, lines)
                start_line = max(pos[0] - 1, 0)
                start_offset = offsets[start_line] if start_line < len(offsets) else 0
                brace_start = java_text.find("{", start_offset)
                if brace_start == -1:
                    return ""
                depth = 0
                for i in range(brace_start, len(java_text)):
                    ch = java_text[i]
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            return java_text[brace_start:i + 1]
                return java_text[brace_start:]
        except Exception:
            pass

        return self._extract_method_body(java_text, method_name)

    def _build_file_variable_dict(self, java_folder: str, file_names) -> dict:
        """
        For each unique file_name from Cleaned_AST_Details, parse the
        corresponding .java file and extract all inline variable=value
        assignments.

        Parameters
        ----------
        java_folder : str
            Root folder of the Java codebase.
        file_names : iterable of str
            Unique values from the ``file_name`` column of Cleaned_AST_Details.
            Each entry is a full or relative path such as
            ``path/to/MyClass.java`` or a stem like ``MyClass``.

        Returns
        -------
        dict
            Structure::

                {
                    "<file_name_as_given>": {
                        "<variable_name>": "<value>",
                        ...
                    },
                    ...
                }
        """
        java_folder = Path(java_folder)
        java_idx = self._get_java_file_indexes(java_folder)
        stem_to_paths = java_idx.get("stem_to_paths", {})
        rel_no_ext_to_path = java_idx.get("rel_no_ext_to_path", {})
        result = {}

        for raw_file_name in file_names:
            raw_file_name = str(raw_file_name)

            # Resolve the actual .java file path.
            # file_name column may be an absolute path, relative path, or stem.
            candidate = Path(raw_file_name)
            if candidate.is_file():
                java_path = candidate.resolve()
            else:
                # Try treating it as a path relative to java_folder
                rel = java_folder / raw_file_name
                if rel.is_file():
                    java_path = rel.resolve()
                else:
                    # Fall back: search by filename stem inside the folder
                    stem = candidate.stem if candidate.suffix else candidate.name
                    rel_key = str(candidate).replace("\\", "/")
                    if rel_key.lower().endswith(".java"):
                        rel_key = rel_key[:-5]
                    java_path = rel_no_ext_to_path.get(rel_key.lower())
                    if java_path is None:
                        matches = stem_to_paths.get(stem.lower(), [])
                        java_path = matches[0] if matches else None
                    if java_path is None:
                        # Nothing found — skip
                        result[raw_file_name] = {}
                        continue
                    java_path = Path(java_path).resolve()

            # Read the file
            try:
                code = java_path.read_text(encoding="utf-8")
            except Exception:
                try:
                    code = java_path.read_text(encoding="latin-1")
                except Exception:
                    result[raw_file_name] = {}
                    continue

            # Extract inline variable assignments from class fields only.
            # This avoids method-local values like hashCode()'s prime/result.
            var_dict = self._extract_inline_field_assignments_ast(code)

            # Fallback regex path only when AST yields nothing.
            if not var_dict:
                for m in self._re_inline_var_assign.finditer(code):
                    var_name = m.group(1)
                    rhs_expr = m.group(2) or ""
                    value = self._collapse_inline_assigned_value(rhs_expr)
                    var_dict[var_name] = value.strip()

            result[raw_file_name] = var_dict

        return result

    def extract_inline_java_variables_as_properties(
        self,
        java_folder: str,
        df_cleaned_ast,
        include_filepath: bool = True,
        include_trailing_dot: bool = True,
    ):
        """
        For codebases that store configuration values directly inside Java
        source files (no separate .properties file), this method:

        1. Reads the ``file_name`` column of *df_cleaned_ast*
           (the Cleaned_AST_Details sheet) to get the list of Java files.
          2. Parses each file and builds a per-file dictionary of
              ``variable → value`` for every inline assignment found.
          3. Builds one global variable dictionary by combining all per-file
              dictionaries from the ``file_name`` set.
        3. For every unique ``(file_name, method_name)`` pair in the sheet,
              extracts the method body and checks which variables from the
              combined dictionary are referenced inside that method.
              If a variable is present in both the same file and another file,
              the same-file value is preferred.
          4. Emits one output row per matched variable, using the same column
           layout as ``extract_application_properties_from_folder`` so the
           two DataFrames can be concatenated and written to the same
           ``application.properties`` Excel sheet.

        Parameters
        ----------
        java_folder : str
            Root directory of the Java codebase.
        df_cleaned_ast : pd.DataFrame
            The Cleaned_AST_Details DataFrame (must have at least
            ``file_name`` and ``method_name`` columns).
        include_filepath : bool
            When True, the ``FilePath`` column is included in the output.
        include_trailing_dot : bool
            When True, ``Filename.methodname`` ends with a trailing dot when
            no method name is available.

        Returns
        -------
        pd.DataFrame
            Same columns as ``extract_application_properties_from_folder``.
        """
        debug_var_name = str(
            (self.details or {}).get("debug_property_variable")
            or os.environ.get("LINEAGE_DEBUG_PROPERTY_VARIABLE")
            or ""
        ).strip()
        debug_var_upper = debug_var_name.upper() if debug_var_name else ""

        def _dbg_var(event: str, **kwargs):
            if not debug_var_upper:
                return
            payload = " | ".join(["{}={}".format(k, kwargs[k]) for k in sorted(kwargs.keys())])
            print("[DEBUG][INLINE_PROPERTY][{}] {}".format(event, payload))

        def _compose(file_name: str, method_name, resolved_path=None) -> str:
            base_path = str(Path(resolved_path).with_suffix("")) if resolved_path else str(Path(file_name).with_suffix(""))
            if method_name:
                return f"{base_path}.{method_name}"
            return f"{base_path}." if include_trailing_dot else base_path

        java_folder = Path(java_folder)
        java_idx = self._get_java_file_indexes(java_folder)
        stem_to_paths = java_idx.get("stem_to_paths", {})
        rel_no_ext_to_path = java_idx.get("rel_no_ext_to_path", {})

        _import_re = re.compile(r'^\s*import\s+([\w.]+)\s*;', re.MULTILINE)
        _import_static_re = re.compile(r'^\s*import\s+static\s+([\w.]+)\s*;', re.MULTILINE)

        def _resolve_java_path(raw_file_name: str):
            """Resolve a raw file token to an actual .java Path when possible."""
            candidate = Path(str(raw_file_name))
            if candidate.is_file():
                return candidate.resolve()

            # Try treating as path relative to java_folder
            rel = java_folder / str(raw_file_name)
            if rel.is_file():
                return rel.resolve()

            # Try appending .java for explicit path-like values without suffix
            if not candidate.suffix:
                c_java = Path(str(candidate) + ".java")
                if c_java.is_file():
                    return c_java.resolve()
                rel_java = java_folder / (str(raw_file_name) + ".java")
                if rel_java.is_file():
                    return rel_java.resolve()

            # Fall back: search by filename stem inside the folder
            stem = candidate.stem if candidate.suffix else candidate.name
            rel_key = str(candidate).replace("\\", "/")
            if rel_key.lower().endswith(".java"):
                rel_key = rel_key[:-5]
            rel_hit = rel_no_ext_to_path.get(rel_key.lower())
            if rel_hit is not None:
                return Path(rel_hit).resolve()

            stem_hits = stem_to_paths.get(stem.lower(), [])
            if stem_hits:
                return Path(stem_hits[0]).resolve()
            return None

        def _resolve_class_java_path(owner: str, caller_code: str, caller_file_name: str):
            """Resolve class source path from explicit import, same package, wildcard or FQN."""
            owner = str(owner or "").strip()
            if not owner:
                return None

            # FQN directly used as owner.
            if "." in owner and owner[0].islower():
                fqn_rel = Path(*owner.split(".")).with_suffix(".java")
                fqn_abs = java_folder / fqn_rel
                if fqn_abs.is_file():
                    return fqn_abs.resolve()
                fqn_name = owner.split(".")[-1]
                fqn_hits = stem_to_paths.get(fqn_name.lower(), [])
                if fqn_hits:
                    return Path(fqn_hits[0]).resolve()

            imports = [x.strip() for x in _import_re.findall(caller_code or "") if x and not x.strip().endswith(".*")]
            import_map = {imp.split(".")[-1]: imp for imp in imports}
            imp_fqn = import_map.get(owner)
            if imp_fqn:
                imp_rel = Path(*imp_fqn.split(".")).with_suffix(".java")
                imp_abs = java_folder / imp_rel
                if imp_abs.is_file():
                    return imp_abs.resolve()

            # Same package fallback.
            pkg_m = re.search(r'^\s*package\s+([\w.]+)\s*;', caller_code or "", re.MULTILINE)
            if pkg_m:
                same_pkg_fqn = pkg_m.group(1) + "." + owner
                same_pkg_rel = Path(*same_pkg_fqn.split(".")).with_suffix(".java")
                same_pkg_abs = java_folder / same_pkg_rel
                if same_pkg_abs.is_file():
                    return same_pkg_abs.resolve()

            # Wildcard imports fallback.
            for imp in _import_re.findall(caller_code or ""):
                imp = (imp or "").strip()
                if not imp.endswith(".*"):
                    continue
                wfqn = imp[:-2] + "." + owner
                wrel = Path(*wfqn.split(".")).with_suffix(".java")
                wabs = java_folder / wrel
                if wabs.is_file():
                    return wabs.resolve()

            # Last fallback by filename.
            owner_hits = stem_to_paths.get(owner.lower(), [])
            if owner_hits:
                return Path(owner_hits[0]).resolve()
            return None

        # ── Step 1: collect unique file names ──────────────────────────
        if "file_name" not in df_cleaned_ast.columns:
            return pd.DataFrame()

        unique_file_names = df_cleaned_ast["file_name"].dropna().unique().tolist()

        # ── Step 2: build per-file variable dictionaries ───────────────
        # IMPORTANT: dictionaries are built only from file_name values.
        file_var_dict = self._build_file_variable_dict(java_folder, unique_file_names)
        # Expose for callers to reuse (avoids rebuilding in export stage).
        self._last_inline_file_var_dict = file_var_dict

        # Build one global dictionary from all file_name files.
        # If duplicate variable names exist across files, the later file in
        # unique_file_names order overwrites the previous global value.
        global_var_dict = {}
        for _fname in unique_file_names:
            _vars = file_var_dict.get(_fname, {})
            if _vars:
                global_var_dict.update(_vars)

        # ── Step 3: resolve actual .java paths for method body lookup ──
        # Build a map: raw_file_name → resolved Path (reuse logic from above)
        file_path_map = {}
        for raw_file_name in unique_file_names:
            resolved = _resolve_java_path(str(raw_file_name))
            if resolved is not None:
                file_path_map[raw_file_name] = resolved

        # Build class-name -> file tokens map so qualified usages like
        # ClassName.VAR can resolve VAR from that class's file dictionary.
        class_to_raw_files = {}
        for raw_file_name in unique_file_names:
            _raw = str(raw_file_name)
            _stem_raw = Path(_raw).stem.lower()
            class_to_raw_files.setdefault(_stem_raw, [])
            if _raw not in class_to_raw_files[_stem_raw]:
                class_to_raw_files[_stem_raw].append(_raw)

            _resolved = file_path_map.get(raw_file_name)
            if _resolved is not None:
                _stem_resolved = _resolved.stem.lower()
                class_to_raw_files.setdefault(_stem_resolved, [])
                if _raw not in class_to_raw_files[_stem_resolved]:
                    class_to_raw_files[_stem_resolved].append(_raw)

        # Cache for file source code (avoid re-reading the same file)
        code_cache = {}

        def _get_code(raw_file_name: str) -> str:
            if raw_file_name in code_cache:
                return code_cache[raw_file_name]
            java_path = file_path_map.get(raw_file_name)
            # Support both lineage file keys and resolved absolute java paths.
            if java_path is None:
                candidate = Path(str(raw_file_name))
                if candidate.is_file():
                    java_path = candidate.resolve()
                else:
                    java_path = _resolve_java_path(str(raw_file_name))
            if java_path is None:
                code_cache[raw_file_name] = ""
                return ""
            try:
                code = java_path.read_text(encoding="utf-8")
            except Exception:
                try:
                    code = java_path.read_text(encoding="latin-1")
                except Exception:
                    code = ""
            code_cache[raw_file_name] = code
            return code

        def _get_or_build_vars_for_source(source_key: str) -> dict:
            """Return parsed variable dict for a source file key/path, with caching."""
            if source_key in file_var_dict:
                return file_var_dict.get(source_key, {}) or {}

            src_code = _get_code(source_key)
            if not src_code:
                file_var_dict[source_key] = {}
                return {}

            vars_map = self._extract_inline_field_assignments_ast(src_code) or {}
            if not vars_map:
                for m_iv in self._re_inline_var_assign.finditer(src_code):
                    vars_map[m_iv.group(1)] = self._collapse_inline_assigned_value(m_iv.group(2) or "").strip()

            file_var_dict[source_key] = vars_map
            return vars_map

        # ── Step 4: iterate targets and extract variable usages ────────
        rows = []

        def _append_rows_for_target(raw_file_name: str, method_name: str, parameter_types_hint: str = ""):
            local_var_dict = file_var_dict.get(raw_file_name, {})
            if not local_var_dict and not global_var_dict:
                return

            code = _get_code(raw_file_name)
            if not code:
                return

            method_body = self._extract_method_body_by_signature(
                code,
                method_name,
                parameter_types_hint=parameter_types_hint,
            )
            if not method_body:
                return

            java_path = file_path_map.get(raw_file_name)
            file_stem = Path(raw_file_name).stem

            # Use class-level variables from the same file first, then
            # fallback to variables declared in other scanned files.
            # Method-local declarations are still excluded because both maps
            # are built from field initializers only.
            effective_var_dict = dict(global_var_dict)
            effective_var_dict.update(local_var_dict)

            # Handle explicit qualified access: ClassName.VAR
            # Resolve VAR from the referenced class file when available.
            qual_pat = re.compile(r'\b([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\b')
            qualified_hits = []
            for qm in qual_pat.finditer(method_body):
                owner = qm.group(1)
                qvar = qm.group(2)
                owner_sources = class_to_raw_files.get(owner.lower(), [])
                for src_raw in owner_sources:
                    src_vars = file_var_dict.get(src_raw, {}) or {}
                    if qvar in src_vars:
                        qualified_hits.append((owner, qvar, src_vars[qvar]))
                        break

                if any(h[0] == owner and h[1] == qvar for h in qualified_hits):
                    continue

                # Owner class may not be in reachable files; resolve via imports/FQN.
                owner_path = _resolve_class_java_path(owner, code, raw_file_name)
                if owner_path is not None:
                    owner_raw = str(owner_path)
                    owner_vars = _get_or_build_vars_for_source(owner_raw)
                    if qvar in owner_vars:
                        qualified_hits.append((owner, qvar, owner_vars[qvar]))

            # Handle static imports: import static a.b.C.CONSTANT;
            # Method may reference CONSTANT directly without ClassName prefix.
            static_import_hits = []
            for imp_static in _import_static_re.findall(code):
                imp_static = (imp_static or "").strip()
                if not imp_static:
                    continue
                if imp_static.endswith(".*"):
                    owner_fqn = imp_static[:-2]
                    owner_cls = owner_fqn.split(".")[-1]
                    owner_path = _resolve_class_java_path(owner_fqn, code, raw_file_name)
                    if owner_path is None:
                        owner_path = _resolve_class_java_path(owner_cls, code, raw_file_name)
                    if owner_path is None:
                        continue
                    owner_raw = str(owner_path)
                    owner_vars = _get_or_build_vars_for_source(owner_raw)
                    for svar, sval in owner_vars.items():
                        if re.search(r'\b' + re.escape(str(svar)) + r'\b', method_body):
                            static_import_hits.append((owner_cls, str(svar), str(sval)))
                    continue

                parts = imp_static.split(".")
                if len(parts) < 2:
                    continue
                svar = parts[-1]
                owner_fqn = ".".join(parts[:-1])
                owner_cls = owner_fqn.split(".")[-1]

                if not re.search(r'\b' + re.escape(svar) + r'\b', method_body):
                    continue

                owner_path = _resolve_class_java_path(owner_fqn, code, raw_file_name)
                if owner_path is None:
                    owner_path = _resolve_class_java_path(owner_cls, code, raw_file_name)
                if owner_path is None:
                    continue

                owner_raw = str(owner_path)
                owner_vars = _get_or_build_vars_for_source(owner_raw)
                if svar in owner_vars:
                    static_import_hits.append((owner_cls, svar, owner_vars[svar]))

            if static_import_hits:
                dedup = []
                seen = set()
                for item in static_import_hits:
                    if item in seen:
                        continue
                    seen.add(item)
                    dedup.append(item)
                static_import_hits = dedup

            static_import_var_names = {name for _, name, _ in static_import_hits}

            for owner, qvar, qval in static_import_hits:
                if debug_var_upper and str(qvar).upper() == debug_var_upper:
                    _dbg_var(
                        "STATIC_IMPORT_HIT",
                        variable=qvar,
                        owner=owner,
                        value=qval,
                        caller_file=raw_file_name,
                        method=method_name,
                    )
                rows.append({
                    "FileName":             file_stem,
                    "FilePath":             str(java_path) if java_path else raw_file_name,
                    "Filename.methodname":  _compose(raw_file_name, method_name, java_path),
                    "Annotation":           "InlineVariable",
                    "Property":             qvar,
                    "Variable":             f"{owner}.{qvar}",
                    "method_name":          method_name,
                    "Actual Value":         qval,
                })

            qualified_var_names = {qv for _, qv, _ in qualified_hits}
            qualified_var_names.update(static_import_var_names)

            for owner, qvar, qval in qualified_hits:
                if debug_var_upper and str(qvar).upper() == debug_var_upper:
                    _dbg_var(
                        "QUALIFIED_HIT",
                        variable=qvar,
                        owner=owner,
                        value=qval,
                        caller_file=raw_file_name,
                        method=method_name,
                    )
                rows.append({
                    "FileName":             file_stem,
                    "FilePath":             str(java_path) if java_path else raw_file_name,
                    "Filename.methodname":  _compose(raw_file_name, method_name, java_path),
                    "Annotation":           "InlineVariable",
                    "Property":             qvar,
                    "Variable":             f"{owner}.{qvar}",
                    "method_name":          method_name,
                    "Actual Value":         qval,
                })

            # Variable matching is intentionally case-sensitive.
            for var_name, var_value in effective_var_dict.items():
                # If explicitly referenced as ClassName.var in this method,
                # keep that owner-resolved value and skip generic fallback.
                if var_name in qualified_var_names:
                    continue
                pattern = re.compile(r'\b' + re.escape(var_name) + r'\b')
                if pattern.search(method_body):
                    if debug_var_upper and str(var_name).upper() == debug_var_upper:
                        _dbg_var(
                            "UNQUALIFIED_HIT",
                            variable=var_name,
                            value=var_value,
                            caller_file=raw_file_name,
                            method=method_name,
                        )
                    rows.append({
                        "FileName":             file_stem,
                        "FilePath":             str(java_path) if java_path else raw_file_name,
                        "Filename.methodname":  _compose(raw_file_name, method_name, java_path),
                        "Annotation":           "InlineVariable",
                        "Property":             var_name,
                        "Variable":             var_name,
                        "method_name":          method_name,
                        "Actual Value":         var_value,
                    })

        # Work with unique (file_name, method_name) pairs to avoid
        # duplicate scanning of the same method body.
        pair_cols = ["file_name", "method_name", "Parameter_Types"]
        available_cols = [c for c in pair_cols if c in df_cleaned_ast.columns]
        if "method_name" not in available_cols:
            return pd.DataFrame()

        unique_pairs = (
            df_cleaned_ast[available_cols]
            .dropna(subset=["method_name"])
            .drop_duplicates()
        )

        for _, pair_row in unique_pairs.iterrows():
            raw_file_name = str(pair_row["file_name"])
            method_name   = str(pair_row["method_name"])
            param_types   = str(pair_row.get("Parameter_Types") or "")
            _append_rows_for_target(raw_file_name, method_name, param_types)

        # ── Step 5: build DataFrame with standard column layout ─────────
        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.drop_duplicates()

            # Render map-like values as stable JSON for readability in output.
            def _format_value(v):
                s = str(v or "").strip()
                if s.startswith("{") and s.endswith("}") and "=" in s:
                    pairs = []
                    for chunk in s[1:-1].split(","):
                        if "=" not in chunk:
                            continue
                        k, vv = chunk.split("=", 1)
                        pairs.append((k.strip(), vv.strip()))
                    if pairs:
                        return json.dumps({k: vv for k, vv in pairs}, ensure_ascii=True)
                return s

            if "Actual Value" in df.columns:
                df["Actual Value"] = df["Actual Value"].apply(_format_value)

            # Persist target constant values for quick debugging outside Excel.
            try:
                _target_prop = "QUERY_WITH_COUNTER_PARTY_CON"
                _target_df = df[df["Property"].astype(str) == _target_prop]
                if not _target_df.empty:
                    out_txt = Path(os.environ.get("LINEAGE_DEBUG_QUERY_VALUE_TXT", "query_with_counter_party_con_value.txt"))
                    lines = []
                    lines.append("Property={}".format(_target_prop))
                    for _rec in _target_df[["FilePath", "Filename.methodname", "Variable", "Actual Value"]].to_dict("records"):
                        lines.append("FilePath={}".format(_rec.get("FilePath", "")))
                        lines.append("Filename.methodname={}".format(_rec.get("Filename.methodname", "")))
                        lines.append("Variable={}".format(_rec.get("Variable", "")))
                        lines.append("Actual Value={}".format(_rec.get("Actual Value", "")))
                        lines.append("-" * 80)
                    out_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
            except Exception:
                pass

            if debug_var_upper and "Property" in df.columns:
                _match_df = df[df["Property"].astype(str).str.upper() == debug_var_upper]
                _dbg_var("FINAL_ROWS", count=len(_match_df))
                for _rec in _match_df[["FilePath", "Filename.methodname", "Property", "Variable", "Actual Value"]].to_dict("records"):
                    _dbg_var(
                        "FINAL_ROW",
                        file_path=_rec.get("FilePath"),
                        filename_method=_rec.get("Filename.methodname"),
                        property=_rec.get("Property"),
                        variable=_rec.get("Variable"),
                        actual_value=_rec.get("Actual Value"),
                    )
        if include_filepath:
            cols = ["FileName", "FilePath", "Filename.methodname", "Annotation",
                    "Property", "Variable", "method_name", "Actual Value"]
        else:
            cols = ["FileName", "Filename.methodname", "Annotation",
                    "Property", "Variable", "method_name", "Actual Value"]
        df = df.reindex(columns=cols)
        return df