export function officialAttestationsFromRoomResponse(data) {
  return data?.official_attestations || [];
}

function roomMatches(activeSnapshot, targetRoomId) {
  const selectedRoomId = String(activeSnapshot?.room?.id || "");
  const expectedRoomId = String(targetRoomId || "");
  return Boolean(expectedRoomId) && selectedRoomId === expectedRoomId;
}

export function applyMaterialToRoomSnapshot(activeSnapshot, targetRoomId, material) {
  if (!roomMatches(activeSnapshot, targetRoomId) || !material?.id) return activeSnapshot;
  const currentMaterials = activeSnapshot.materials || [];
  const exists = currentMaterials.some((item) => item.id === material.id);
  return {
    ...activeSnapshot,
    materials: exists
      ? currentMaterials.map((item) => item.id === material.id ? material : item)
      : [material, ...currentMaterials],
  };
}

export function applyOfficialAttestationToRoomSnapshot(activeSnapshot, targetRoomId, attestation) {
  if (!roomMatches(activeSnapshot, targetRoomId) || !attestation?.id) return activeSnapshot;
  const currentAttestations = activeSnapshot.official_attestations || [];
  const exists = currentAttestations.some((item) => item.id === attestation.id);
  return {
    ...activeSnapshot,
    official_attestations: exists
      ? currentAttestations.map((item) => item.id === attestation.id ? attestation : item)
      : [attestation, ...currentAttestations],
  };
}
