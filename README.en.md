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

An initial Python scaffold provides window discovery, application launch and focus, screenshot capture, and a configurable vision gateway client. See the [development guide](docs/development.md) for setup. End-to-end visual recognition and action verification still require desktop validation.

An experimental chat and relay workflow adds configured navigation, visible-message collection, SQLite observations, and one-way summaries with sender attribution. The standalone program has completed one desktop relay test; continuous operation and broader message coverage still require validation.

The [burst-recovery design](docs/backfill-design.md) adds ordered anchors, overlapping scroll recovery, explicit gaps, a separate summary worker, and durable queues. Tests cover a simulated 1,000-message burst and eight real multiline test messages; sustained high-volume operation still requires validation.

In one desktop A/B run on the same eight messages, calibrated batch scrolling reduced captures from 122 to 75 and elapsed time from 66.1 to 56.1 seconds, with no missing or duplicate test messages. Results depend on the client and machine load.

[Human takeover](docs/human-takeover.md) pauses desktop input during user activity and requires fresh observation before resuming. Interrupted sends preserve drafts or remain uncertain rather than being retried blindly. A native input self-test and a real-window scrolling check passed; long-term desktop sharing still requires validation.

Experiments should use accounts and environments owned by the user or used with permission. Compatibility and platform acceptance must be evaluated; undetectability and freedom from account restrictions are not guaranteed.

Technology should give people more time, more choice, and more control.
