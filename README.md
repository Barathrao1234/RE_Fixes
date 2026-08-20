_re_inline_var_assign = re.compile(
    r'(?:private|public|protected|static|final|\s)+'
    r'[\w<>\[\]]+\s+'          # type
    r'([A-Za-z_]\w*)'          # variable name (group 1)
    r'\s*=\s*'
    r'(.*?);',                 # everything up to the terminating ; (group 2)
    re.DOTALL,
)

---------------

var_name = m.group(1)
# Join multi-line concatenated string parts into one value
raw_val = m.group(2) or ""
parts = [p.strip().strip('"\'') for p in raw_val.split("+")]
value = " ".join(p for p in parts if p)
var_dict[var_name] = value.strip()
