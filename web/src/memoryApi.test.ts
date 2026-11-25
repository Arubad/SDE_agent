import { describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import { applyConsent, type LearnerSettings } from "./memoryApi";

const saved: LearnerSettings = { consent_personal: false, consent_raw_messages: true, retention_days: 90 };

describe("applyConsent", () => {
  it("saves straight away when the server accepts", async () => {
    const put = vi.fn().mockResolvedValue(saved);
    expect(await applyConsent(put, { consent_personal: false })).toEqual({ kind: "saved", settings: saved });
    expect(put).toHaveBeenCalledWith({ consent_personal: false });
  });

  it("asks for confirmation on a 409 and resends with confirm_purge once confirmed", async () => {
    const put = vi.fn().mockRejectedValueOnce(new ApiError(409, "Turning personal memory off deletes 3 remembered facts."));
    const first = await applyConsent(put, { consent_personal: false });
    expect(first).toEqual({ kind: "confirm", message: "Turning personal memory off deletes 3 remembered facts." });
    put.mockResolvedValueOnce(saved);
    await applyConsent(put, { consent_personal: false }, true);
    expect(put).toHaveBeenLastCalledWith({ consent_personal: false, confirm_purge: true });
  });

  it("lets other errors through", async () => {
    const put = vi.fn().mockRejectedValue(new ApiError(422, "bad"));
    await expect(applyConsent(put, { retention_days: 0 })).rejects.toThrow("bad");
  });
});
