# ruff: noqa: E501
import json
import sys

# fakes built from repeated patterns
A16 = "ABCD" * 4
AKID = "AKIA" + A16  # AKIA + 16
AWS_SECRET = "aBcDeFgHiJ" * 4  # 40 chars
ALNUM36 = "a1B2c3D4e5F6" * 3
SESSION = "FwoGZXIvYXdzE" + "Ab1Cd2Ef3Gh4" * 12
B64_88 = ("Zm9vYmFyYmF6" * 7)[:86] + "=="
JWT = "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiJmYWtlLXVzZXIifQ" + "." + "ZmFrZXNpZ25hdHVyZWZha2VzaWduYXR1cmU"
PEM_BODY = [
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7",
    "Zm9vYmFyZmFrZWtleW1hdGVyaWFsZm9vYmFyZmFrZWtleW1h",
    "dGVyaWFsZm9vYmFyZmFrZWtleW1hdGVyaWFs",
]
NPMB64 = "ZmFrZXVzZXI6ZmFrZXBhc3N3b3Jk"
cases = []


def add(id, tpl, secrets, keep=()):
    if isinstance(secrets, str):
        secrets = [secrets]
    cases.append({"id": id, "template": tpl, "secrets": list(secrets), "keep": list(keep)})


add(
    "aws_akid_env",
    "Run terraform apply\nAWS_ACCESS_KEY_ID={S0}\nAWS_REGION=us-east-1",
    [AKID],
    ["AWS_ACCESS_KEY_ID=", "AWS_REGION=us-east-1"],
)
add(
    "aws_secret_env",
    "export AWS_SECRET_ACCESS_KEY={S0}\nterraform init",
    [AWS_SECRET],
    ["export AWS_SECRET_ACCESS_KEY=", "terraform init"],
)
add(
    "aws_credentials_file",
    "[default]\naws_access_key_id = {S0}\naws_secret_access_key = {S1}",
    [AKID, AWS_SECRET],
    ["[default]", "aws_access_key_id =", "aws_secret_access_key ="],
)
add(
    "aws_json_creds",
    'Credentials: {"AccessKeyId": "{S0}", "SecretAccessKey": "{S1}", "Expiration": "2026-09-08T04:08:44Z"}',
    [AKID, AWS_SECRET],
    ['"AccessKeyId"', '"SecretAccessKey"', '"Expiration": "2026-09-08T04:08:44Z"'],
)
add(
    "aws_bare_secret_after_id",
    "using static credentials {S0} {S1} for sts",
    [AKID, AWS_SECRET],
    ["using static credentials", "for sts"],
)
add(
    "aws_session_token",
    "AWS_SESSION_TOKEN={S0}\nsts get-caller-identity",
    [SESSION],
    ["AWS_SESSION_TOKEN=", "sts get-caller-identity"],
)
add(
    "aws_presigned_url",
    "GET https://bucket.s3.amazonaws.com/key?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature={S0} 403",
    ["0123456789abcdef" * 4],
    ["https://bucket.s3.amazonaws.com/key?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=", "403"],
)
add(
    "aws_sigv4_header",
    "Authorization: AWS4-HMAC-SHA256 Credential={S0}/20260908/us-east-1/s3/aws4_request, SignedHeaders=host, Signature={S1}",
    [AKID, "fedcba9876543210" * 4],
    ["SignedHeaders=host", "us-east-1/s3/aws4_request"],
)
for pre in ("ghp_", "gho_", "ghs_", "ghu_", "ghr_"):
    add(
        "github_" + pre.rstrip("_"),
        "GITHUB_TOKEN: {S0}\nfetching refs",
        [pre + ALNUM36],
        ["GITHUB_TOKEN:", "fetching refs"],
    )
add(
    "github_pat",
    "gh auth login --with-token <<< {S0}\nLogged in",
    ["github_pat_" + "11ABCDEFG0" + "abcdefghij" * 5 + "_" + "ZYXWVUTSRQ" * 3],
    ["gh auth login --with-token <<<", "Logged in"],
)
add(
    "github_clone_url",
    "git clone https://x-access-token:{S0}@github.com/acme/app.git\nCloning into 'app'...",
    ["ghs_" + ALNUM36],
    ["https://x-access-token:", "@github.com/acme/app.git", "Cloning into 'app'..."],
)
add(
    "github_token_as_username",
    "remote: https://{S0}@github.com/acme/app.git not found",
    ["ghp_" + ALNUM36],
    ["@github.com/acme/app.git not found"],
)
add(
    "github_auth_header",
    "> Authorization: token {S0}\n< HTTP/2 401",
    ["gho_" + ALNUM36],
    ["Authorization: token", "< HTTP/2 401"],
)
add("gitlab_pat", "GITLAB_TOKEN={S0}", ["glpat-" + "AbCdEfGh1234" * 2], ["GITLAB_TOKEN="])
add(
    "slack_bot",
    "SLACK_BOT_TOKEN={S0} channel=#ci",
    ["xoxb-" + "123456789012-" * 2 + "AbCdEfGhIjKlMnOpQrStUvWx"],
    ["SLACK_BOT_TOKEN=", "channel=#ci"],
)
add(
    "slack_user",
    "slack: using {S0} to post",
    ["xoxp-" + "123456789012-" * 3 + "abcdef0123456789abcdef0123456789"],
    ["slack: using", "to post"],
)
add(
    "slack_app",
    "app token {S0} rejected",
    ["xapp-1-A0123456789-1234567890123-" + "ab12" * 8],
    ["app token", "rejected"],
)
add(
    "slack_webhook",
    "curl -X POST https://hooks.slack.com/services/{S0} -d x",
    ["T00000000/B00000000/" + "XXXXXXXXXXXXXXXXXXXXXXXX"],
    ["curl -X POST https://hooks.slack.com/services/", "-d x"],
)
add(
    "stripe_live",
    "stripe.api_key = '{S0}'\nStripe error: rate limited",
    ["sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"],
    ["stripe.api_key =", "Stripe error: rate limited"],
)
add(
    "stripe_restricted",
    "using key {S0} for refunds",
    ["rk_live_" + "51H8aBcDeFgHiJkLmNoPqRsT"],
    ["using key", "for refunds"],
)
add("stripe_webhook", "STRIPE_WEBHOOK_SECRET={S0}", ["whsec_" + "AbCd1234EfGh5678IjKl9012"], ["STRIPE_WEBHOOK_SECRET="])
add(
    "google_api_key",
    "GET https://maps.googleapis.com/maps/api/geocode/json?key={S0} 403 Forbidden",
    ["AIza" + "SyAbCdEfGhIjKlMnOpQrStUvWxYz0123456"],
    ["https://maps.googleapis.com/maps/api/geocode/json?key=", "403 Forbidden"],
)
add(
    "google_oauth_access",
    "oauth access value: {S0} (expired)",
    ["ya29." + "a0AfH6SMBxAbCdEfGhIjKl_0123456789"],
    ["oauth access value:", "(expired)"],
)
add(
    "gcp_sa_json",
    '{"type": "service_account", "project_id": "demo-project", "private_key": "-----BEGIN PRIVATE KEY-----\\n{S0}\\n-----END PRIVATE KEY-----\\n", "client_email": "ci@demo-project.iam.gserviceaccount.com"}',
    ["\\n".join(PEM_BODY)],
    [
        '"type": "service_account"',
        '"project_id": "demo-project"',
        '"private_key":',
        '"client_email": "ci@demo-project.iam.gserviceaccount.com"',
    ],
)
add(
    "pem_rsa_multiline",
    "key file contents follow\n-----BEGIN RSA PRIVATE KEY-----\n"
    + "\n".join(PEM_BODY)
    + "\n-----END RSA PRIVATE KEY-----\nend of key",
    PEM_BODY,
    ["key file contents follow", "end of key"],
)
add(
    "pem_openssh_multiline",
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    + "\n".join(PEM_BODY)
    + "\n-----END OPENSSH PRIVATE KEY-----\nIdentity added",
    PEM_BODY,
    ["Identity added"],
)
add(
    "pem_ec_multiline",
    "-----BEGIN EC PRIVATE KEY-----\n" + "\n".join(PEM_BODY) + "\n-----END EC PRIVATE KEY-----",
    PEM_BODY,
)
add(
    "pem_pgp",
    "-----BEGIN PGP PRIVATE KEY BLOCK-----\n"
    + "\n".join(PEM_BODY)
    + "\n-----END PGP PRIVATE KEY BLOCK-----\ngpg: key imported",
    PEM_BODY,
    ["gpg: key imported"],
)
add(
    "pem_truncated",
    "ssh key dump:\n-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(PEM_BODY[:2]),
    PEM_BODY[:2],
    ["ssh key dump:"],
)
add("jwt_bearer", "Authorization: Bearer {S0}\nHTTP/1.1 401", [JWT], ["Authorization: Bearer", "HTTP/1.1 401"])
add("jwt_bare", "id_token={S0} expired", [JWT], ["id_token=", "expired"])
add(
    "npm_authtoken_line",
    "//registry.npmjs.org/:_authToken={S0}\nnpm ERR! code E401",
    ["npm_" + ALNUM36],
    ["//registry.npmjs.org/:_authToken=", "npm ERR! code E401"],
)
add(
    "npm_authtoken_uuid",
    "//npm.pkg.github.com/:_authToken={S0}",
    ["0a1b2c3d-4e5f-6a7b-8c9d-0e1f2a3b4c5d"],
    ["//npm.pkg.github.com/:_authToken="],
)
add(
    "npm_auth_base64",
    "//registry.example.com/:_auth={S0}\nalways-auth=true",
    [NPMB64],
    ["//registry.example.com/:_auth=", "always-auth=true"],
)
add("npm_token_in_text", "npm publish with {S0} failed", ["npm_" + ALNUM36], ["npm publish with", "failed"])
add(
    "docker_config_auth",
    '{"auths": {"https://index.docker.io/v1/": {"auth": "{S0}"}}}',
    [NPMB64],
    ['"auths"', '"https://index.docker.io/v1/"', '"auth":'],
)
add(
    "docker_login_flag",
    "docker login -u ci-user -p {S0} registry.example.com\nLogin Succeeded",
    ["Pa55w0rdFake99"],
    ["docker login -u ci-user -p", "registry.example.com", "Login Succeeded"],
)
add("docker_pat", "DOCKERHUB_TOKEN={S0}", ["dckr_pat_" + "AbCdEfGhIjKl_0123456789"], ["DOCKERHUB_TOKEN="])
add(
    "basic_auth_url",
    "Error: failed to fetch https://deploy:{S0}@registry.example.com/v2/ : 401",
    ["p4ssw0rdFakeValue"],
    ["https://deploy:", "@registry.example.com/v2/ : 401"],
)
add(
    "basic_auth_url_pip",
    "Looking in indexes: https://__token__:{S0}@pypi.example.com/simple",
    ["fake-index-secret-value"],
    ["https://__token__:", "@pypi.example.com/simple"],
)
add(
    "authorization_basic",
    "-H 'Authorization: Basic {S0}' https://api.example.com",
    [NPMB64],
    ["Authorization: Basic", "https://api.example.com"],
)
add(
    "authorization_bearer_json",
    '"Authorization": "Bearer {S0}"',
    ["fakeTokenValue0123456789abcdefABCDEF"],
    ['"Authorization": "Bearer'],
)
add(
    "curl_user",
    "curl -u admin:{S0} https://nexus.example.com/repository/x",
    ["fakeNexusPassw0rd"],
    ["curl -u admin:", "https://nexus.example.com/repository/x"],
)
add(
    "postgres_url",
    "psql: error: connection to postgres://app:{S0}@db.internal:5432/orders failed",
    ["Fake-Db-Passw0rd"],
    ["postgres://app:", "@db.internal:5432/orders failed"],
)
add(
    "mongodb_srv",
    "MongoServerError: bad auth for mongodb+srv://svc:{S0}@cluster0.example.mongodb.net/test",
    ["FakeMongoPassw0rd"],
    ["mongodb+srv://svc:", "@cluster0.example.mongodb.net/test"],
)
add(
    "redis_empty_user",
    "redis://:{S0}@cache.internal:6379/0 NOAUTH",
    ["FakeRedisPassw0rd"],
    ["redis://:", "@cache.internal:6379/0 NOAUTH"],
)
add(
    "mysql_flag",
    "mysql -h db -u root -p{S0} -e select1",
    ["FakeMysqlPassw0rd"],
    ["mysql -h db -u root -p", "-e select1"],
)
add(
    "jdbc_password_param",
    "jdbc:postgresql://db:5432/app?user=app&password={S0}&ssl=true",
    ["FakeJdbcPassw0rd"],
    ["jdbc:postgresql://db:5432/app?user=app&password=", "&ssl=true"],
)
add(
    "azure_storage_key",
    "DefaultEndpointsProtocol=https;AccountName=demostore;AccountKey={S0};EndpointSuffix=core.windows.net",
    [B64_88],
    ["AccountName=demostore;AccountKey=", "EndpointSuffix=core.windows.net"],
)
add(
    "azure_sas",
    "GET https://demostore.blob.core.windows.net/c/b?sv=2022-11-02&sig={S0}&se=2026-12-31 403",
    ["AbCdEfGhIjKlMnOpQrStUvWxYz0123456789%2BAbCdEf%3D"],
    ["https://demostore.blob.core.windows.net/c/b?sv=2022-11-02&sig=", "&se=2026-12-31 403"],
)
add(
    "azure_client_secret",
    "AZURE_CLIENT_SECRET={S0}\nAZURE_TENANT_ID=11111111-1111-1111-1111-111111111111",
    ["Abc8Q~fakeFAKEfake0123456789-_.AbCdEf"],
    ["AZURE_CLIENT_SECRET=", "AZURE_TENANT_ID=11111111-1111-1111-1111-111111111111"],
)
add(
    "sql_pwd_conn",
    "Server=db.internal;Database=app;Uid=svc;Pwd={S0};",
    ["FakeSqlPassw0rd"],
    ["Server=db.internal;Database=app;Uid=svc;Pwd="],
)
add(
    "anthropic_key",
    "ANTHROPIC_API_KEY={S0}\nerror: 401 authentication_error",
    ["sk-ant-api03-" + "AbCdEfGh0123_-" * 6 + "AA"],
    ["ANTHROPIC_API_KEY=", "error: 401 authentication_error"],
)
add(
    "anthropic_in_text",
    "request failed with key {S0} (invalid x-api-key)",
    ["sk-ant-api03-" + "ZyXwVuTs9876_-" * 6 + "AA"],
    ["request failed with key", "(invalid x-api-key)"],
)
add(
    "openai_key",
    "OPENAI key {S0} hit quota",
    ["sk-" + "AbCd1234EfGh5678IjKl9012MnOp3456QrSt7890UvWx"],
    ["OPENAI key", "hit quota"],
)
add(
    "openai_proj_key",
    "using sk-proj key: {S0}",
    ["sk-proj-" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv1Wx2Yz3"],
    ["using sk-proj key:"],
)
add(
    "sendgrid",
    "SENDGRID_API_KEY={S0}",
    ["SG." + "AbCdEfGhIjKlMnOpQr" + "." + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdE"],
    ["SENDGRID_API_KEY="],
)
add(
    "pypi_token",
    "twine upload -u __token__ -p {S0}",
    ["pypi-" + "AgEIcHlwaS5vcmc" + "AbCdEfGh0123" * 5],
    ["twine upload -u __token__ -p"],
)
add(
    "generic_password_colon",
    "database config\npassword: {S0}\nhost: db.internal",
    ["hunter2hunter2"],
    ["password:", "host: db.internal"],
)
add(
    "generic_password_quoted_spaces",
    'password: "{S0}"\nport: 5432',
    ["correct horse battery staple"],
    ["password:", "port: 5432"],
)
add(
    "generic_password_flag",
    "mytool login --user ci --password {S0} --retries 3",
    ["fl4gPassw0rd"],
    ["mytool login --user ci --password", "--retries 3"],
)
add(
    "generic_password_flag_eq",
    "mytool login --password={S0} --retries 3",
    ["fl4gPassw0rdEq"],
    ["mytool login --password=", "--retries 3"],
)
add("generic_token_flag", "deploy --token {S0} --env prod", ["tok3nValue0123"], ["deploy --token", "--env prod"])
add(
    "json_quoted_secret",
    '{"client_id": "abc", "client_secret": "{S0}", "grant_type": "client_credentials"}',
    ["s3cr3tValue-with space"],
    ['"client_id": "abc"', '"client_secret":', '"grant_type": "client_credentials"'],
)
add("json_api_key", '{"apiKey": "{S0}", "region": "eu"}', ["Zx9Qm4Lp7Rt2Kv8Nb5Cw3Yd6"], ['"apiKey":', '"region": "eu"'])
add(
    "env_dump",
    "DB_HOST=db.internal\nDB_PASSWORD={S0}\nPWD=/home/runner/work/app\nHOME=/home/runner\nSECRET_KEY_BASE={S1}\nPATH=/usr/bin",
    ["hunter2fake", "Zx9Qm4Lp7Rt2Kv8Nb5Cw3Yd6Hf1Js0Ga"],
    [
        "DB_HOST=db.internal",
        "DB_PASSWORD=",
        "PWD=/home/runner/work/app",
        "HOME=/home/runner",
        "SECRET_KEY_BASE=",
        "PATH=/usr/bin",
    ],
)
add(
    "env_dump_xapikey",
    "x-api-key: {S0}\ncontent-type: application/json",
    ["fakeApiKeyValue0123"],
    ["x-api-key:", "content-type: application/json"],
)
add(
    "k8s_secret_data",
    "data:\n  password: {S0}\n  username: ci",
    ["Zm9vYmFyZmFrZXBhc3N3b3Jk"],
    ["data:", "password:", "username: ci"],
)
add(
    "terraform_hcl",
    'client_secret = "{S0}"\nregion = "us-east-1"',
    ["Fake_Client_Secret.0123456789"],
    ["client_secret =", 'region = "us-east-1"'],
)
add("base64_after_secret_name", "secret: {S0}", ["c2VjcmV0LXZhbHVlLWZha2UtMDEyMzQ1Njc4OQ=="], ["secret:"])
add(
    "entropy_loose_key",
    "TOKEN_VALUE={S0}\nbuild ok",
    ["Zx9Qm4Lp7Rt2Kv8Nb5Cw3Yd6Hf1Js0Ga"],
    ["TOKEN_VALUE=", "build ok"],
)
add("entropy_loose_key_colon", "secret_blob: {S0}", ["Qw3Er5Ty7Ui9Op1As2Df4Gh6Jk8Lz0Xc"], ["secret_blob:"])
add(
    "entropy_auth_name",
    "x-auth-signature-value={S0}",
    ["Mn4Bv6Cx8Za0Sd2Fg5Hj7Kl9Qw1Er3Ty"],
    ["x-auth-signature-value="],
)
add("tf_var_password", "TF_VAR_db_password={S0}", ["Fake_Tf_Passw0rd_01"], ["TF_VAR_db_password="])
add("session_key_env", "SESSION_KEY={S0}", ["fakeSessionKeyValue0123"], ["SESSION_KEY="])

nofp = [
    "commit 3a4f5b6c7d8e9f001122334455667788aabbccdd",
    "HEAD is now at 0123456789abcdef0123456789abcdef01234567 fix lockfile",
    "request id 123e4567-e89b-12d3-a456-426614174000 failed",
    "execution_id: 9f8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d",
    "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "integrity sha512-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIjKlMnOp==",
    "md5sum: d41d8cd98f00b204e9800998ecf8427e  package.json",
    "terraform v1.5.7 on linux_amd64 + provider registry.terraform.io/hashicorp/aws v5.31.0",
    "npm ERR! node v20.11.1 npm v10.2.4",
    "Python 3.11.7 pip 23.3.1 setuptools==69.0.3",
    "arn:aws:iam::123456789012:role/ci-deployer is not authorized to perform: sts:AssumeRole",
    "User: arn:aws:sts::123456789012:assumed-role/ci/session-abc is not authorized to perform: secretsmanager:GetSecretValue on resource: arn:aws:secretsmanager:us-east-1:123456789012:secret:prod/db-creds-AbCdEf",
    "arn:aws:s3:::my-bucket/path/to/object.tar.gz",
    "arn:aws:kms:us-east-1:123456789012:key/1234abcd-12ab-34cd-56ef-1234567890ab",
    "AccessDenied: not authorized to perform secretsmanager:GetSecretValue",
    "PWD=/home/runner/work/app/app",
    "Error: Unable to resolve action actions/checkout@v4, unable to find version",
    "image: ghcr.io/acme/app@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "Token expired, please re-authenticate",
    "The security token included in the request is expired",
    "git clone git@github.com:acme/app.git",
    "ssh://git@github.com:22/acme/app.git",
    "Run actions/setup-node@60edb5dd545a775178f52524783378180af0d1f8",
]
doc = {
    "_comment": "Every secret below is FAKE: built from repeated patterns in the correct real-world format so detectors fire. None is, or was ever, a real credential. Placeholders {S0},{S1} in a template are replaced by the matching entry in secrets.",
    "cases": cases,
    "no_false_positives": nofp,
}
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=1, ensure_ascii=False)
print(len(cases))
