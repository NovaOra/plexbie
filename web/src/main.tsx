import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import App from "./App";
import { MotionConfig } from "motion/react";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {/* Reduced motion: movement and layout animations are dropped, fades kept. */}
    <MotionConfig reducedMotion="user">
      <App />
    </MotionConfig>
  </StrictMode>,
);
