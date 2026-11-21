import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { TaskLogOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { TaskLogModal } from "./TaskLogModal";

vi.mock("../api/resources", () => ({
  pbsInstancesApi: { taskLog: vi.fn() },
}));

const { pbsInstancesApi } = await import("../api/resources");

const LOG: TaskLogOut = {
  lines: [
    { n: 1, text: "starting backup" },
    { n: 2, text: "TASK OK" },
  ],
  truncated: false,
};

describe("TaskLogModal", () => {
  it("shows a loading state, then the fetched log", async () => {
    let resolveLog: (log: TaskLogOut) => void = () => {};
    vi.mocked(pbsInstancesApi.taskLog).mockReturnValue(
      new Promise((resolve) => {
        resolveLog = resolve;
      }),
    );
    renderWithProviders(
      <TaskLogModal open instanceId="pbs-1" upid="UPID:pbs:1:backup:root@pam:" onClose={() => {}} />,
    );

    expect(screen.getByText("Loading…")).toBeInTheDocument();
    resolveLog(LOG);

    await waitFor(() => expect(screen.getByText(/starting backup/)).toBeInTheDocument());
    expect(screen.getByText(/TASK OK/)).toBeInTheDocument();
    expect(pbsInstancesApi.taskLog).toHaveBeenCalledWith("pbs-1", "UPID:pbs:1:backup:root@pam:");
  });

  it("shows the truncation notice when the log was cut off", async () => {
    vi.mocked(pbsInstancesApi.taskLog).mockResolvedValue({ ...LOG, truncated: true });
    renderWithProviders(
      <TaskLogModal open instanceId="pbs-1" upid="UPID:pbs:1:backup:root@pam:" onClose={() => {}} />,
    );

    await waitFor(() =>
      expect(
        screen.getByText("This log has more lines than shown here — showing the most recent ones."),
      ).toBeInTheDocument(),
    );
  });

  it("shows an error state when the fetch fails", async () => {
    vi.mocked(pbsInstancesApi.taskLog).mockRejectedValue(new Error("boom"));
    renderWithProviders(
      <TaskLogModal open instanceId="pbs-1" upid="UPID:pbs:1:backup:root@pam:" onClose={() => {}} />,
    );

    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getByText(/Something went wrong/)).toBeInTheDocument());
  });

  it("calls onClose when the close button is clicked", async () => {
    vi.mocked(pbsInstancesApi.taskLog).mockResolvedValue(LOG);
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(
      <TaskLogModal open instanceId="pbs-1" upid="UPID:pbs:1:backup:root@pam:" onClose={onClose} />,
    );

    await waitFor(() => expect(screen.getByText(/starting backup/)).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("does not fetch when closed", () => {
    renderWithProviders(
      <TaskLogModal open={false} instanceId="pbs-1" upid="UPID:pbs:1:backup:root@pam:" onClose={() => {}} />,
    );
    expect(pbsInstancesApi.taskLog).not.toHaveBeenCalled();
  });
});
