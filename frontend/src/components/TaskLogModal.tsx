import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { pbsInstancesApi } from "../api/resources";
import { InlineAlert } from "./InlineAlert";
import { Modal } from "./Modal";
import { QueryState } from "./QueryState";
import styles from "./TaskLogModal.module.css";

interface TaskLogModalProps {
  open: boolean;
  instanceId: string;
  upid: string;
  onClose: () => void;
}

/** The "window box" for a task's log, opened by clicking its UPID on the
 * Jobs pages (manifest §17). Shows the *tail* of the log, not the head —
 * for a failed task the actual error is almost always near the end (see
 * `connectivity.pbs_api.get_task_log`'s docstring); `truncated` tells the
 * operator the log has more lines than are shown. */
export function TaskLogModal({ open, instanceId, upid, onClose }: TaskLogModalProps) {
  const { t } = useTranslation();
  const logQuery = useQuery({
    queryKey: ["task-log", instanceId, upid],
    queryFn: () => pbsInstancesApi.taskLog(instanceId, upid),
    enabled: open,
  });

  return (
    <Modal open={open} title={t("tasks.logTitle")} onClose={onClose}>
      <p className={`resurgam-mono ${styles.upid}`}>{upid}</p>
      {/* `enabled: open` leaves the query permanently idle (no data, not
       * loading, not errored) while closed — QueryState assumes a query
       * always eventually settles, so only render it while actually open. */}
      {open && (
        <QueryState query={logQuery}>
          {(log) => (
            <>
              {log.truncated && <InlineAlert tone="info">{t("tasks.logTruncated")}</InlineAlert>}
              <pre className={styles.logBlock}>{log.lines.map((line) => line.text).join("\n")}</pre>
            </>
          )}
        </QueryState>
      )}
    </Modal>
  );
}
