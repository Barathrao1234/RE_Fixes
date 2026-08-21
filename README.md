            mkw = re.match(r'^\s*(return|this|super|new)\s*\.\s*([A-Za-z_]\w*)(.*)$', obj_call, flags=re.IGNORECASE)
            if mkw:
                keyword = mkw.group(1).lower()
                meth = mkw.group(2)
                rest = mkw.group(3) or ""
                if keyword == "super":
                    _pc = strip_generics(parent_class)
                    _super_cls = method_return_index.get(_pc, {}).get("__extends__") or _pc
                    return "{}.{}{}".format(_super_cls, meth, rest)
                return "{}.{}{}".format(strip_generics(parent_class), meth, rest)
