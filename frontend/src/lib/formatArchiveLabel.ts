/** A VM disk's top-level tree entry is its raw PBS archive name (e.g.
 * `drive-scsi0.img.fidx`) -- strips the technical suffix for display
 * (`drive-scsi0.img.fidx` -> `scsi0`), matching how an operator actually
 * thinks about disks ("scsi0", not the archive filename PBS stores it
 * under). Falls back to the raw name unchanged if it doesn't match the
 * expected `drive-<name>.img.fidx` shape (e.g. never seen in practice,
 * but real disk-naming variants aren't worth guessing at).
 */
export function formatArchiveLabel(archive: string): string {
  const match = /^drive-(.+)\.img\.fidx$/.exec(archive);
  return match ? match[1] : archive;
}
