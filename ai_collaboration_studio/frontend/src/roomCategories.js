export function roomCategoryPath(room) {
  const apiPath = Array.isArray(room?.category_path)
    ? room.category_path.map((part) => String(part || "").trim()).filter(Boolean)
    : [];
  if (apiPath.length) return apiPath;
  const fallback = String(room?.category || "通用共创")
    .replaceAll("／", "/")
    .split("/")
    .map((part) => part.trim())
    .filter(Boolean);
  return fallback.length ? fallback : ["通用共创"];
}

export function groupedRooms(rooms, search = "") {
  const needle = search.trim().toLocaleLowerCase("zh-CN");
  const groups = new Map();
  for (const room of rooms || []) {
    const path = roomCategoryPath(room);
    const searchable = [room?.title, room?.objective, ...path]
      .map((value) => String(value || ""))
      .join(" ")
      .toLocaleLowerCase("zh-CN");
    if (needle && !searchable.includes(needle)) continue;
    const root = path[0];
    if (!groups.has(root)) groups.set(root, []);
    groups.get(root).push({
      ...room,
      category_path: path,
      subcategory_label: path.slice(1).join(" / ") || "群聊",
    });
  }
  return [...groups].map(([name, categoryRooms]) => ({ name, rooms: categoryRooms }));
}
