# CSS Question Bank

## CSS cascade

Explain how the CSS cascade determines which rule is applied when multiple rules target the same element. Expected points: origin, importance, cascade layers, specificity, source order, inheritance, and how to avoid excessive specificity.

## Specificity

Explain CSS specificity and how to calculate which selector wins. Expected points: inline styles, IDs, classes and attributes, elements and pseudo-elements, specificity comparison, `!important`, source order, and strategies for keeping specificity manageable.

## Box model

Explain the CSS box model. Expected points: content, padding, border, margin, `box-sizing`, difference between `content-box` and `border-box`, how dimensions are calculated, and why `border-box` is commonly preferred.

## Flexbox

Explain how Flexbox works and when you would use it. Expected points: flex container and items, main and cross axes, `flex-direction`, `justify-content`, `align-items`, `flex-wrap`, `flex`, `gap`, and appropriate one-dimensional layout use cases.

## CSS Grid

Compare CSS Grid with Flexbox. Expected points: two-dimensional versus one-dimensional layouts, rows and columns, grid tracks, areas, `fr` units, `gap`, responsive layouts, and situations where Grid is preferable to Flexbox.

## Positioning

Explain the different CSS positioning modes. Expected points: `static`, `relative`, `absolute`, `fixed`, and `sticky`, containing blocks, offsets, scrolling behavior, stacking contexts, and common use cases.

## Responsive design

How do you build a responsive web page using CSS? Expected points: mobile-first design, media queries, flexible layouts, relative units, Grid and Flexbox, responsive typography, responsive images, breakpoints based on content rather than devices, and avoiding fixed-width layouts.

## CSS units

Compare `px`, `%`, `em`, `rem`, `vw`, `vh`, and newer viewport units. Expected points: absolute versus relative units, inheritance behavior, root-relative sizing, viewport-relative sizing, responsive typography, and choosing units based on the design requirement.

## CSS inheritance

Explain CSS inheritance and how it differs from the cascade. Expected points: properties that inherit by default, properties that do not inherit, `inherit`, `initial`, `unset`, `revert`, controlling inheritance, and how inheritance can simplify component styling.

## Pseudo-classes and pseudo-elements

Explain the difference between pseudo-classes and pseudo-elements. Expected points: states such as `:hover`, `:focus`, and `:nth-child`, generated or conceptual parts such as `::before` and `::after`, syntax differences, accessibility considerations, and practical use cases.

## Stacking context and z-index

Explain why a high `z-index` does not always place an element above another element. Expected points: stacking contexts, positioned elements, `z-index`, properties that create new stacking contexts, parent-child stacking behavior, and debugging layering problems.

## CSS animations and transitions

Compare CSS transitions and animations. Expected points: triggering transitions, keyframes, animation timing, duration, iteration, easing, transform and opacity, performance considerations, and respecting `prefers-reduced-motion`.

## CSS performance

How do you optimize CSS for production? Expected points: reducing unused CSS, minimizing stylesheet size, avoiding expensive selectors, reducing layout and paint work, critical CSS, caching, CSS organization, and measuring performance instead of optimizing blindly.

## CSS architecture

How would you organize CSS for a large application? Expected points: component-based organization, naming conventions such as BEM, design tokens, CSS layers, avoiding global leakage, reusable utilities, theming, specificity management, and maintainability across teams.

## CSS accessibility

How can CSS negatively affect accessibility, and how do you prevent it? Expected points: visible focus states, sufficient contrast, reduced motion, avoiding content conveyed only through color, text resizing, logical reading order, hiding versus removing content, and ensuring visual styling does not break keyboard or screen-reader usage.