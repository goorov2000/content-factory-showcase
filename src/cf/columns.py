def apply_column_aliases(rows, aliases):
    if not aliases:
        return rows
    out = []
    for r in rows:
        row = dict(r)
        for canonical, actual in aliases.items():
            if canonical not in row and actual in row:
                row[canonical] = row[actual]
        out.append(row)
    return out
