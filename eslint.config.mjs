import js from '@eslint/js';
import globals from 'globals';

export default [
  // Only the tracked JavaScript is linted, so a run from the root never walks build output or a
  // working copy nested inside the checkout.
  {
    ignores: [
      '*',
      '!functions/',
      '!scripts/',
      '!explorer/',
      '!pipeline/',
      'pipeline/*',
      '!pipeline/publicdata/',
      'pipeline/publicdata/*',
      '!pipeline/publicdata/static/',
      '!eslint.config.mjs',
    ],
  },
  js.configs.recommended,
  {
    linterOptions: {
      reportUnusedDisableDirectives: 'error',
      reportUnusedInlineConfigs: 'error',
    },
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
    },
    rules: {
      'array-callback-return': 'error',
      'consistent-return': 'error',
      curly: ['error', 'all'],
      'default-case-last': 'error',
      eqeqeq: ['error', 'always'],
      'no-caller': 'error',
      'no-console': 'error',
      'no-constructor-return': 'error',
      'no-eval': 'error',
      'no-extend-native': 'error',
      'no-implicit-coercion': ['error', { allow: ['!!'] }],
      'no-implicit-globals': 'error',
      'no-implied-eval': 'error',
      'no-labels': 'error',
      'no-lone-blocks': 'error',
      'no-loop-func': 'error',
      'no-multi-assign': 'error',
      'no-new': 'error',
      'no-new-func': 'error',
      'no-new-wrappers': 'error',
      'no-object-constructor': 'error',
      'no-param-reassign': 'error',
      'no-promise-executor-return': 'error',
      'no-proto': 'error',
      'no-return-assign': ['error', 'always'],
      'no-script-url': 'error',
      'no-self-compare': 'error',
      'no-sequences': 'error',
      'no-shadow': 'error',
      'no-template-curly-in-string': 'error',
      'no-throw-literal': 'error',
      'no-undef-init': 'error',
      'no-unmodified-loop-condition': 'error',
      'no-unreachable-loop': 'error',
      'no-unused-expressions': 'error',
      'no-unused-vars': ['error', { args: 'all', caughtErrors: 'all', ignoreRestSiblings: false }],
      'no-use-before-define': ['error', { functions: false, variables: false }],
      'no-useless-call': 'error',
      'no-useless-computed-key': 'error',
      'no-useless-concat': 'error',
      'no-useless-rename': 'error',
      'no-useless-return': 'error',
      'no-var': 'error',
      'object-shorthand': ['error', 'properties'],
      'prefer-const': 'error',
      'prefer-promise-reject-errors': 'error',
      'prefer-rest-params': 'error',
      'prefer-spread': 'error',
      radix: 'error',
      strict: ['error', 'safe'],
      yoda: 'error',
    },
  },
  {
    files: ['functions/**/*.js'],
    languageOptions: { globals: globals.serviceworker },
  },
  {
    files: ['functions/**/*.test.mjs'],
    languageOptions: { globals: globals.node },
  },
  {
    files: ['scripts/**/*.mjs', 'eslint.config.mjs'],
    languageOptions: { globals: globals.node },
  },
  {
    files: ['scripts/**/*.mjs'],
    rules: { 'no-console': 'off' },
  },
  // The house checks are functions Puppeteer serialises into the page, so they run with its globals.
  {
    files: ['scripts/a11y-checks.mjs'],
    languageOptions: { globals: { ...globals.node, ...globals.browser } },
  },
  // site.js runs on every page as written, so it keeps the ES5 syntax older browsers parse. The
  // rules that need later syntax are off for it.
  {
    files: ['pipeline/publicdata/static/site.js'],
    languageOptions: {
      ecmaVersion: 5,
      sourceType: 'script',
      globals: { ...globals.browser, Promise: 'readonly' },
    },
    rules: {
      'no-unused-vars': ['error', { args: 'all', caughtErrors: 'none', ignoreRestSiblings: false }],
      'no-var': 'off',
      'object-shorthand': 'off',
      'prefer-const': 'off',
      'prefer-rest-params': 'off',
      'prefer-spread': 'off',
    },
  },
  {
    files: ['pipeline/publicdata/static/explorer.js'],
    languageOptions: { ecmaVersion: 2020, globals: globals.browser },
  },
  // esbuild bundles this for es2022.
  {
    files: ['explorer/**/*.js'],
    languageOptions: { ecmaVersion: 2022, globals: globals.browser },
  },
];
