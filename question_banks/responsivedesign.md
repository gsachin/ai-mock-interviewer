# Responsive Design Question Bank

## Mobile-first design

Explain the mobile-first approach to responsive web design. Expected points: designing for smaller screens first, progressive enhancement, base styles for mobile, media queries for larger screens, performance benefits, content prioritization, and why mobile-first is different from simply targeting mobile devices.

## Responsive breakpoints

How do you decide which breakpoints to use in a responsive application? Expected points: content-driven breakpoints, avoiding device-specific assumptions, common viewport ranges, testing between breakpoints, CSS media queries, and why too many breakpoints increase complexity.

## Fluid layouts

Explain how to build a fluid responsive layout. Expected points: percentage-based dimensions, flexible containers, `max-width`, `min-width`, Flexbox, Grid, relative units, `clamp()`, avoiding unnecessary fixed widths, and maintaining readable content across viewport sizes.

## Responsive typography

How do you implement responsive typography? Expected points: relative units such as `rem`, viewport units, `clamp()`, minimum and maximum font sizes, line height, readable measure, accessibility, browser zoom, and avoiding typography that becomes too small on mobile devices.

## Responsive images

How should images be handled in a responsive website? Expected points: flexible image sizing, `max-width`, `height: auto`, `srcset`, `sizes`, `picture`, modern image formats, art direction, lazy loading, intrinsic dimensions, and preventing layout shifts.

## Responsive navigation

How would you design navigation that works across desktop and mobile? Expected points: desktop versus mobile navigation patterns, menu buttons, accessible keyboard interaction, focus management, semantic navigation, responsive CSS, avoiding hidden-but-inaccessible content, and maintaining consistent navigation behavior.

## CSS media queries

Explain how CSS media queries are used to create responsive interfaces. Expected points: viewport conditions, `min-width`, `max-width`, orientation, resolution, combining conditions, mobile-first queries, media features, and avoiding excessive device-specific rules.

## Container queries

Explain CSS container queries and how they differ from media queries. Expected points: component-based responsiveness, container context, `container-type`, `@container`, reusable components, differences from viewport-based responsiveness, and situations where container queries are more appropriate.

## Responsive Flexbox

How can Flexbox be used to create responsive layouts? Expected points: flexible sizing, `flex-grow`, `flex-shrink`, `flex-basis`, wrapping, `gap`, alignment, changing direction at breakpoints, handling variable content, and avoiding fixed dimensions that cause overflow.

## Responsive CSS Grid

How can CSS Grid create responsive layouts without many media queries? Expected points: `repeat()`, `minmax()`, `auto-fit`, `auto-fill`, fractional units, implicit versus explicit tracks, responsive cards, grid areas, and choosing Grid for two-dimensional layouts.

## Responsive tables

How would you make a large data table usable on small screens? Expected points: horizontal scrolling, responsive column prioritization, stacked layouts, preserving table semantics, avoiding unreadable text, handling long values, accessibility, and choosing an appropriate presentation based on the data.

## Responsive forms

How do you design forms that work well across desktop and mobile devices? Expected points: flexible widths, appropriate input types, labels, touch-friendly controls, vertical layouts on smaller screens, validation messages, keyboard behavior, avoiding excessive horizontal layouts, and accessibility.

## Touch and pointer interaction

What should developers consider when designing responsive interfaces for touch devices? Expected points: touch target size, spacing, pointer types, hover limitations, gestures, accidental activation, `pointer` media queries, keyboard accessibility, and providing alternatives to hover-only interactions.

## Responsive performance

How does responsive design affect web performance? Expected points: responsive images, lazy loading, avoiding unnecessary assets, conditional resource loading, CSS efficiency, mobile network constraints, critical rendering path, Core Web Vitals, and measuring performance on real devices.

## Responsive testing

How do you test a responsive web application across different devices and viewport sizes? Expected points: browser developer tools, real-device testing, orientation changes, different pixel densities, touch interaction, accessibility testing, network conditions, intermediate viewport widths, and testing content extremes rather than only standard device sizes.