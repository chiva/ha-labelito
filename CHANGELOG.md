# Changelog

## [1.3.0](https://github.com/chiva/ha-labelito/compare/v1.2.0...v1.3.0) (2026-09-03)


### Features

* **voice:** add a dry-run option for Assist-driven prints ([#39](https://github.com/chiva/ha-labelito/issues/39)) ([1335c12](https://github.com/chiva/ha-labelito/commit/1335c121857ee5af3a284eb90ba4c52e6094e6e6))
* **voice:** add a dry-run option for Assist-driven prints ([#42](https://github.com/chiva/ha-labelito/issues/42)) ([3c3e5e3](https://github.com/chiva/ha-labelito/commit/3c3e5e3b5f97d4baaeed5f067b7576294455f9be))
* **voice:** match spoken template names against a generated closed list ([#41](https://github.com/chiva/ha-labelito/issues/41)) ([4fb4899](https://github.com/chiva/ha-labelito/commit/4fb48990982d79c062a737e9ed55a54579e7e3fc))


### Bug Fixes

* **services:** never make a dry run the reprint-last target ([#38](https://github.com/chiva/ha-labelito/issues/38)) ([cd492f9](https://github.com/chiva/ha-labelito/commit/cd492f9648f180da1f2bb43c45d64891db9bcf3e))
* **voice:** harden template matching against real speech-to-text output ([#37](https://github.com/chiva/ha-labelito/issues/37)) ([530e6e0](https://github.com/chiva/ha-labelito/commit/530e6e015db960d941465971b58f834262fe084a))

## [1.2.0](https://github.com/chiva/ha-labelito/compare/v1.1.0...v1.2.0) (2026-08-31)


### Features

* support https connections to the labelito service ([#34](https://github.com/chiva/ha-labelito/issues/34)) ([bc1f031](https://github.com/chiva/ha-labelito/commit/bc1f031ae506a09c2883c4bd48c5c0a155a0a9d3))

## [1.1.0](https://github.com/chiva/ha-labelito/compare/v1.0.1...v1.1.0) (2026-07-21)


### Features

* add high_res, threshold, and inline template print options ([#27](https://github.com/chiva/ha-labelito/issues/27)) ([26f59be](https://github.com/chiva/ha-labelito/commit/26f59bed925518699f6310d76caf3a5f937e5c44))

## [1.0.1](https://github.com/chiva/ha-labelito/compare/v1.0.0...v1.0.1) (2026-07-10)


### Bug Fixes

* recover spoken text folded into the Assist template wildcard ([#20](https://github.com/chiva/ha-labelito/issues/20)) ([799c040](https://github.com/chiva/ha-labelito/commit/799c04050033c80bc314a3779a6cdb279f33a47f))
* **voice:** recover Spanish free text + ship custom_sentences/ + README branding ([#22](https://github.com/chiva/ha-labelito/issues/22)) ([d467d72](https://github.com/chiva/ha-labelito/commit/d467d7288830f57370098cadfb42ca7ccf9abc38))

## [1.0.0](https://github.com/chiva/ha-labelito/compare/v0.2.0...v1.0.0) (2026-07-10)


### ⚠ BREAKING CHANGES

* the integration now requires a labelito service speaking API version 3; v1/v2 servers are rejected at setup.

### Features

* target labelito API v3 and add {{seq}} auto-numbering ([#18](https://github.com/chiva/ha-labelito/issues/18)) ([f65b6aa](https://github.com/chiva/ha-labelito/commit/f65b6aac1260e83d0b3ac8ad745a532718a79b13))

## [0.2.0](https://github.com/chiva/ha-labelito/compare/v0.1.0...v0.2.0) (2026-07-05)


### Features

* bundle brand images for HACS and Home Assistant ([#13](https://github.com/chiva/ha-labelito/issues/13)) ([bc1846b](https://github.com/chiva/ha-labelito/commit/bc1846bebe08dbffd747b47eeed2c9d50b98f8a6))

## [0.1.0](https://github.com/chiva/ha-labelito/compare/v0.1.0...v0.1.0) (2026-07-05)


### Features

* initial release ([#4](https://github.com/chiva/ha-labelito/issues/4)) ([7e11001](https://github.com/chiva/ha-labelito/commit/7e11001f135e089a3f529a7d1aca027ef8e2e348))
