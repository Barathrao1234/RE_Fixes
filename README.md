        def normalize_keyword_rooted_call(s, parent_class):
            if not isinstance(s, str) or not s.strip():
                return s
            s = s.strip()
            m = re.match(r'^\s*(return|this|super|new)\s*\.\s*([A-Za-z_]\w*)(.*)$', s, flags=re.IGNORECASE)
            if m:
                keyword = m.group(1).lower()
                meth = m.group(2)
                rest = m.group(3) or ""
                if keyword == "super":
                    # super.method() means the method lives in the PARENT class,
                    # not the current class. Walk __extends__ to find it.
                    _pc = strip_generics(parent_class)
                    _super_cls = method_return_index.get(_pc, {}).get("__extends__") or _pc
                    return "{}.{}{}".format(_super_cls, meth, rest).strip()
                return "{}.{}{}".format(strip_generics(parent_class), meth, rest).strip()
            return s
