import type { DataMessagePartProps } from "@assistant-ui/react";

/** Renders {type:"data", name:"error"} parts (the `error` ChatEvent). */
export function ErrorBanner({ data }: DataMessagePartProps<{ message: string }>) {
  return (
    <div className="cwyc-error-banner" role="alert">
      <strong>Error:</strong> {data.message}
    </div>
  );
}
