IMAGE = s3transfer-fork

.PHONY: build
build:
	docker build --pull -t $(IMAGE) .

.PHONY: test
test: build
	docker run $(IMAGE) scripts/ci/run-tests


.PHONY: publish
publish:
	@docker run $(IMAGE) python3 manage.py upload --username $(DEVPI_USERNAME) --password $(DEVPI_PASSWORD) --index https://pypi.lundalogik.com:3443/lime/develop/+simple/
