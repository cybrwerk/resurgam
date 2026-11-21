import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { renderWithProviders } from "../test/testUtils";
import { Wizard, type WizardStep } from "./Wizard";

const STEPS: WizardStep[] = [
  { id: "one", title: "Step One" },
  { id: "two", title: "Step Two" },
  { id: "three", title: "Step Three" },
];

function Harness({ initialCanAdvance = true }: { initialCanAdvance?: boolean }) {
  const [step, setStep] = useState(0);
  return (
    <Wizard
      steps={STEPS}
      currentStep={step}
      onStepClick={setStep}
      canAdvance={initialCanAdvance}
      onBack={() => setStep((s) => Math.max(0, s - 1))}
      onNext={() => setStep((s) => Math.min(STEPS.length - 1, s + 1))}
      nextLabel="Finish"
    >
      <div>Content for {STEPS[step].title}</div>
    </Wizard>
  );
}

describe("Wizard", () => {
  it("renders the first step's content and disables Back", async () => {
    renderWithProviders(<Harness />);
    expect(screen.getByText("Content for Step One")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Back" })).toBeDisabled();
  });

  it("advances to the next step on Next", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Harness />);

    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText("Content for Step Two")).toBeInTheDocument();
  });

  it("shows the custom label on the last step instead of Next", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Harness />);

    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByRole("button", { name: "Finish" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Next" })).not.toBeInTheDocument();
  });

  it("disables Next when canAdvance is false", () => {
    renderWithProviders(<Harness initialCanAdvance={false} />);
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("does not render a clickable button for future steps", () => {
    renderWithProviders(<Harness />);
    // "Step Two"/"Step Three" appear as plain labels, not buttons, since
    // the user hasn't reached them yet.
    expect(screen.queryByRole("button", { name: "Step Two" })).not.toBeInTheDocument();
    expect(screen.getByText("Step Two")).toBeInTheDocument();
  });

  it("lets the user click back to an already-completed step", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Harness />);

    await user.click(screen.getByRole("button", { name: "Next" })); // now on step 2
    await user.click(screen.getByRole("button", { name: /Step One/ }));
    expect(screen.getByText("Content for Step One")).toBeInTheDocument();
  });
});
