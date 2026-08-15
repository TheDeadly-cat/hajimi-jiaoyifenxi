export function createLatestRequestCoordinator() {
  let sequence = 0;
  let controller = null;
  let inFlight = false;
  let activeLoadingChange = null;

  const run = async ({
    forceRequest = false,
    request,
    onSuccess = () => {},
    onError = () => {},
    onLoadingChange = () => {},
  }) => {
    if (inFlight && !forceRequest) {
      return { status: "skipped", started: false, applied: false };
    }

    const requestSequence = sequence + 1;
    sequence = requestSequence;
    controller?.abort();
    controller = new AbortController();
    inFlight = true;
    activeLoadingChange = onLoadingChange;
    onLoadingChange(true);

    try {
      const value = await request(controller.signal);
      if (sequence !== requestSequence) {
        return { status: "stale", started: true, applied: false };
      }
      await onSuccess(value);
      return { status: "applied", started: true, applied: true, value };
    } catch (requestError) {
      if (sequence !== requestSequence || requestError?.name === "AbortError") {
        return { status: "stale", started: true, applied: false };
      }
      await onError(requestError);
      return { status: "error", started: true, applied: false, error: requestError };
    } finally {
      if (sequence === requestSequence) {
        inFlight = false;
        controller = null;
        activeLoadingChange = null;
        onLoadingChange(false);
      }
    }
  };

  const cancel = () => {
    sequence += 1;
    controller?.abort();
    controller = null;
    inFlight = false;
    const notifyLoadingChange = activeLoadingChange;
    activeLoadingChange = null;
    notifyLoadingChange?.(false);
  };

  return {
    run,
    cancel,
    get inFlight() {
      return inFlight;
    },
  };
}
