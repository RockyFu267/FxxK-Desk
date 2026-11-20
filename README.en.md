# FxxK-Desk

[中文](README.md) | [English](README.en.md)

**Make desktop software work for people.**

**FxxK** stands for **Freedom · eXploration · eXpertise · Knowledge**. **Desk** is where we put those values into practice: giving users more choice, turning exploration into useful tools, and applying shared knowledge and engineering expertise to everyday work.

FxxK-Desk explores automation for desktop applications with limited APIs or integration options. Our goal is to give users more control over their own workflows through visual understanding and desktop interaction.

## What we want to build

- **See and understand:** use screenshots, OCR, and vision models to interpret interfaces and application state.
- **Act and verify:** click, type, scroll, and manage windows, then check the results.
- **Keep working:** observe changes, trigger tasks, track progress, and recover from interruptions.
- **Explore freely:** combine RPA, visual agents, and new interaction methods, including frameworks we design ourselves.

## Approach

Build around a simple loop: **Observe → Understand → Act → Verify**.

Start with Windows. Combine visual perception, local AI, mouse and keyboard control, and available system interfaces. Choose methods through practical experiments and measured results.

Keep the core independent of application adapters, process data locally by default, and make components replaceable. Begin with one useful workflow, then improve reliability and expand to more applications.

## Status

Early exploration; no runnable implementation yet. The first milestone is to identify a target window, capture and recognize its contents, and complete a small workflow with a verifiable result.

Experiments should use accounts and environments owned by the user or used with permission. Compatibility and platform acceptance must be evaluated; undetectability and freedom from account restrictions are not guaranteed.

Technology should give people more time, more choice, and more control.
