Entity normalization rules for entity_v2:

1. Output only [ENTITY]. Keep [RELATION] and [LOGIC_GROUP] empty.
2. The normalized_name is the relation key. It must match the current annotation style, not a newly invented synonym.
3. Do not over-abstract concrete fault events. Keep specific symptoms and object scope when they are in the text.
4. For fault events, keep component/object identity plus the fault phenomenon, such as leakage, crack, wear, ablation, coating loss, interference, abnormal pressure, failed connection, or efficiency decrease.
5. Remove raw measurement values from normalized_name when they are only evidence details, but keep growth/change/severity words when they change the event meaning.
6. For maintenance methods, keep action plus target object. Preserve meaningful modifiers such as "重新", "用新备件", and specific inspection method when present.
7. For trigger rules, keep the comparator and threshold/setting, such as "高于", "低于", "超过", "达到", or "不满足". Do not output only the parameter name.
8. For alarm codes, normalize to the code identifier only. Remove values, units, and readings after "=".
9. Do not replace a specific phrase with a broad category. For example, keep "断断续续“嘣嘣”的声音" instead of "异响" if the text uses the concrete symptom.
10. Prefer one entity per real-world item; merge repeated mentions into one entity line with multiple evidence items.
