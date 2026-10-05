import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { MotionConfig } from "motion/react";
import "../src/styles.css";
import ProjectApp from "../src/project/ProjectApp";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {/* Reduced motion: movement and layout animations are dropped, fades kept. */}
    <MotionConfig reducedMotion="user">
      <ProjectApp />
    </MotionConfig>
  </StrictMode>,
);
