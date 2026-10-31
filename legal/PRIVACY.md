# Verdandi Privacy Policy

Applies to the Verdandi desktop application ("the app"). Last updated:
2026-10-30.

## The short version

Your documents stay on your machine. Page images go only to the AI model
provider you configure with your own key. The app does **not** send
documents, filenames, extracted values, install IDs, analytics or usage data
to the project maintainers.

## What the app sends

### 1. Your model provider

To read a document, the app sends **page images** of the PDF and the
extraction prompt to the OpenAI-compatible endpoint configured in
`config/models.yaml`, authenticated with **your own gateway key**. That
processing is governed by your provider's privacy policy and billing terms.
The maintainers never see your documents or your key; the key is stored in
your operating system's credential store.

### 2. Auto-update (official builds only)

Official installers check the public GitHub releases feed for a newer
version, and download and install it only when you approve. That request is
a standard HTTPS call to GitHub, which necessarily sees your IP address and
app version. Builds made from source can remove or change the updater
endpoint in `desktop/src-tauri/tauri.conf.json`.

### 3. Nothing else

There is no access check, no telemetry, no analytics, no advertising SDK and
no feedback upload. The app does not create an install identifier.

## What stays local

Runs, page renders, extracted data, review decisions, exports and logs stay
in your workspace folder (`~/.verdandi` on macOS and Linux,
`%USERPROFILE%\.verdandi` on Windows). Removing the app and that folder
removes the data.

## Third-party services

Your model provider and GitHub (for updates) are independent services. Their
handling of data is described by their own policies. The app ships no other
network integration.

## Changes

Material changes will be noted in the release notes and in this file.
