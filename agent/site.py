import json
import os
import re
import shutil
import time
from datetime import datetime

import requests

from agent.base import Base
from agent.job import job, step
from agent.utils import get_size, b2mb


class Site(Base):
    def __init__(self, name, bench):
        self.name = name
        self.bench = bench
        self.directory = os.path.join(self.bench.sites_directory, name)
        self.backup_directory = os.path.join(self.directory, ".migrate")
        self.logs_directory = os.path.join(self.directory, "logs")
        self.config_file = os.path.join(self.directory, "site_config.json")
        self.touched_tables_file = os.path.join(
            self.directory, "touched_tables.json"
        )
        self.analytics_file = os.path.join(
            self.directory,
            "analytics.json",
        )

        if not os.path.isdir(self.directory):
            raise OSError(f"Path {self.directory} is not a directory")

        if not os.path.exists(self.config_file):
            raise OSError(f"Path {self.config_file} does not exist")

        self.database = self.config["db_name"]
        self.user = self.config["db_name"]
        self.password = self.config["db_password"]
        self.host = self.config.get("db_host", self.bench.host)

    def bench_execute(self, command, input=None):
        return self.bench.docker_execute(
            f"bench --site {self.name} {command}", input=input
        )

    def dump(self):
        return {"name": self.name}

    @step("Rename Site")
    def rename(self, new_name):
        os.rename(
            self.directory, os.path.join(self.bench.sites_directory, new_name)
        )
        self.name = new_name

    @job("Rename Site", priority="high")
    def rename_job(self, new_name):
        self.enable_maintenance_mode()
        self.wait_till_ready()
        if self.config.get("host_name") == f"https://{self.name}":
            self.update_config({"host_name": f"https://{new_name}"})
        self.rename(new_name)
        self.bench.setup_nginx()
        self.bench.server.reload_nginx()
        self.disable_maintenance_mode()
        self.enable_scheduler()

    @step("Install Apps")
    def install_apps(self, apps):
        data = {"apps": {}}
        output = []
        for app in apps:
            data["apps"][app] = {}
            log = data["apps"][app]
            if app != "frappe":
                log["install"] = self.bench_execute(f"install-app {app}")
                output.append(log["install"]["output"])
        return data

    @step("Install App on Site")
    def install_app(self, app):
        return self.bench_execute(f"install-app {app}")

    @step("Uninstall App from Site")
    def uninstall_app(self, app):
        return self.bench_execute(f"uninstall-app {app} --yes --force")

    @step("Restore Site")
    def restore(
        self,
        mariadb_root_password,
        admin_password,
        database_file,
        public_file,
        private_file,
    ):
        sites_directory = self.bench.sites_directory
        database_file = database_file.replace(
            sites_directory, "/home/frappe/frappe-bench/sites"
        )
        public_file = public_file.replace(
            sites_directory, "/home/frappe/frappe-bench/sites"
        )
        private_file = private_file.replace(
            sites_directory, "/home/frappe/frappe-bench/sites"
        )

        public_file_option = (
            f"--with-public-files {public_file}" if public_file else ""
        )
        private_file_option = (
            f"--with-private-files {private_file} " if private_file else ""
        )

        _, temp_user, temp_password = self.bench.create_mariadb_user(
            self.name, mariadb_root_password, self.database
        )
        try:
            return self.bench_execute(
                "--force restore "
                f"--mariadb-root-username {temp_user} "
                f"--mariadb-root-password {temp_password} "
                f"--admin-password {admin_password} "
                f"{public_file_option} "
                f"{private_file_option} "
                f"{database_file}"
            )
        finally:
            self.bench.drop_mariadb_user(
                self.name, mariadb_root_password, self.database
            )

    @job("Restore Site")
    def restore_job(
        self,
        apps,
        mariadb_root_password,
        admin_password,
        database,
        public,
        private,
        skip_failing_patches,
    ):
        files = self.bench.download_files(self.name, database, public, private)
        try:
            self.restore(
                mariadb_root_password,
                admin_password,
                files["database"],
                files["public"],
                files["private"],
            )
        finally:
            self.bench.delete_downloaded_files(files["directory"])
        self.uninstall_unavailable_apps(apps)
        self.migrate(skip_failing_patches=skip_failing_patches)
        self.set_admin_password(admin_password)
        self.enable_scheduler()

        self.bench.setup_nginx()
        self.bench.server.reload_nginx()

        return self.bench_execute("list-apps")

    @job("Migrate Site")
    def migrate_job(self, skip_failing_patches=False):
        return self.migrate(skip_failing_patches=skip_failing_patches)

    @step("Reinstall Site")
    def reinstall(
        self,
        mariadb_root_password,
        admin_password,
    ):
        _, temp_user, temp_password = self.bench.create_mariadb_user(
            self.name, mariadb_root_password, self.database
        )
        try:
            return self.bench_execute(
                f"reinstall --yes "
                f"--mariadb-root-username {temp_user} "
                f"--mariadb-root-password {temp_password} "
                f"--admin-password {admin_password}"
            )
        finally:
            self.bench.drop_mariadb_user(
                self.name, mariadb_root_password, self.database
            )

    @job("Reinstall Site")
    def reinstall_job(
        self,
        mariadb_root_password,
        admin_password,
    ):
        return self.reinstall(mariadb_root_password, admin_password)

    @job("Install App on Site")
    def install_app_job(self, app):
        self.install_app(app)

    @job("Uninstall App on Site")
    def uninstall_app_job(self, app):
        self.uninstall_app(app)

    @step("Update Site Configuration")
    def update_config(self, value, remove=None):
        """Pass Site Config value to update or replace existing site config.

        Args:
            value (dict): Site Config
            remove (list, optional): Keys sent in the form of a list will be
                popped from the existing site config. Defaults to None.
        """
        new_config = self.config
        new_config.update(value)

        if remove:
            for key in remove:
                new_config.pop(key, None)

        self.setconfig(new_config)

    @job("Add Domain", priority="high")
    def add_domain(self, domain):
        domains = set(self.config.get("domains", []))
        domains.add(domain)
        self.update_config({"domains": list(domains)})
        self.bench.setup_nginx()
        self.bench.server.reload_nginx()

    @job("Remove Domain", priority="high")
    def remove_domain(self, domain):
        domains = set(self.config.get("domains", []))
        domains.discard(domain)
        self.update_config({"domains": list(domains)})
        self.bench.setup_nginx()
        self.bench.server.reload_nginx()

    @job("Setup ERPNext", priority="high")
    def setup_erpnext(self, user, config):
        self.create_user(
            user["email"],
            user["first_name"],
            user["last_name"],
        )
        self.update_erpnext_config(config)
        return {"sid": self.sid(user["email"])}

    @job("Restore Site Tables", priority="high")
    def restore_site_tables_job(self, activate):
        self.restore_site_tables()
        if activate:
            self.disable_maintenance_mode()

    @step("Restore Site Tables")
    def restore_site_tables(self):
        data = {"tables": {}}
        for backup_file in os.listdir(self.backup_directory):
            backup_file_path = os.path.join(self.backup_directory, backup_file)
            output = self.execute(
                f"mysql -h {self.host} -u {self.user} -p{self.password} "
                f"{self.database} < '{backup_file_path}'"
            )
            data["tables"][backup_file] = output
        return data

    @step("Update ERPNext Configuration")
    def update_erpnext_config(self, value):
        config_file = os.path.join(self.directory, "journeys_config.json")
        with open(config_file, "r") as f:
            config = json.load(f)

        config.update(value)

        with open(config_file, "w") as f:
            json.dump(config, f, indent=1, sort_keys=True)

    @step("Create User")
    def create_user(self, email, first_name, last_name):
        return self.bench_execute(
            f"add-system-manager {email} "
            f"--first-name {first_name} --last-name {last_name}"
        )

    @job("Update Site Configuration", priority="high")
    def update_config_job(self, value, remove):
        self.update_config(value, remove)

    @job("Update Saas Plan")
    def update_saas_plan(self, plan):
        self.update_plan(plan)

    @step("Update Saas Plan")
    def update_plan(self, plan):
        self.bench_execute(f"update-site-plan {plan}")

    @step("Backup Site")
    def backup(self, with_files=False):
        with_files = "--with-files" if with_files else ""
        self.bench_execute(f"backup {with_files}")
        return self.fetch_latest_backup(with_files=with_files)

    @step("Upload Site Backup to S3")
    def upload_offsite_backup(self, backup_files, offsite):
        import boto3

        offsite_files = {}
        bucket, auth, prefix = (
            offsite["bucket"],
            offsite["auth"],
            offsite["path"],
        )
        s3 = boto3.client(
            "s3",
            aws_access_key_id=auth["ACCESS_KEY"],
            aws_secret_access_key=auth["SECRET_KEY"],
        )

        for backup_file in backup_files.values():
            file_name = backup_file["file"].split(os.sep)[-1]
            offsite_path = os.path.join(prefix, file_name)
            offsite_files[file_name] = offsite_path

            with open(backup_file["path"], "rb") as data:
                s3.upload_fileobj(data, bucket, offsite_path)

        return offsite_files

    @step("Enable Maintenance Mode")
    def enable_maintenance_mode(self):
        return self.bench_execute("set-maintenance-mode on")

    @step("Set Administrator Password")
    def set_admin_password(self, password):
        return self.bench_execute(f"set-admin-password {password}")

    @step("Wait for Enqueued Jobs")
    def wait_till_ready(self):
        WAIT_TIMEOUT = 120
        data = {"tries": []}
        start = time.time()
        while (time.time() - start) < WAIT_TIMEOUT:
            try:
                output = self.bench_execute("ready-for-migration")
                data["tries"].append(output)
                break
            except Exception as e:
                data["tries"].append(e.data)
                time.sleep(1)
        return data

    @step("Clear Backup Directory")
    def clear_backup_directory(self):
        if os.path.exists(self.backup_directory):
            shutil.rmtree(self.backup_directory)
        os.mkdir(self.backup_directory)

    @step("Backup Site Tables")
    def tablewise_backup(self):
        data = {"tables": {}}
        for table in self.tables:
            backup_file = os.path.join(self.backup_directory, f"{table}.sql")
            output = self.execute(
                "mysqldump --single-transaction --quick --lock-tables=false "
                f"-h {self.host} -u {self.user} -p{self.password} "
                f"{self.database} '{table}' "
                f"> '{backup_file}'"
            )
            data["tables"][table] = output
        return data

    @step("Migrate Site")
    def migrate(self, skip_failing_patches=False):
        if skip_failing_patches:
            return self.bench_execute("migrate --skip-failing")
        else:
            return self.bench_execute("migrate")

    @job("Clear Cache")
    def clear_cache_job(self):
        self.clear_cache()
        self.clear_website_cache()

    @step("Clear Cache")
    def clear_cache(self):
        return self.bench_execute("clear-cache")

    @step("Clear Website Cache")
    def clear_website_cache(self):
        return self.bench_execute("clear-website-cache")

    @step("Uninstall Unavailable Apps")
    def uninstall_unavailable_apps(self, apps_to_keep):
        installed_apps = json.loads(
            self.bench_execute("execute frappe.get_installed_apps")["output"]
        )
        for app in installed_apps:
            if app not in apps_to_keep:
                self.bench_execute(f"remove-from-installed-apps '{app}'")
                self.bench_execute("clear-cache")

    @step("Disable Maintenance Mode")
    def disable_maintenance_mode(self):
        try:
            self.bench_execute(
                "execute frappe.modules.patch_handler.block_user "
                "--args False,",
            )
        except Exception:
            pass
        return self.bench_execute("set-maintenance-mode off")

    @step("Restore Touched Tables")
    def restore_touched_tables(self):
        data = {"tables": {}}
        for table in self.touched_tables:
            backup_file = os.path.join(self.backup_directory, f"{table}.sql")
            if os.path.exists(backup_file):
                output = self.execute(
                    f"mysql -h {self.host} -u {self.user} -p{self.password} "
                    f"{self.database} < '{backup_file}'"
                )
                data["tables"][table] = output
        return data

    @step("Pause Scheduler")
    def pause_scheduler(self):
        return self.bench_execute("scheduler pause")

    @step("Enable Scheduler")
    def enable_scheduler(self):
        return self.bench_execute("scheduler enable")

    @step("Resume Scheduler")
    def resume_scheduler(self):
        return self.bench_execute("scheduler resume")

    def fetch_site_status(self):
        data = {
            "scheduler": True,
            "web": True,
            "timestamp": str(datetime.now()),
        }
        try:
            ping_url = f"https://{self.name}/api/method/ping"
            data["web"] = requests.get(ping_url).status_code == 200
        except Exception:
            data["web"] = False

        doctor = self.bench_execute("doctor")
        if "inactive" in doctor["output"]:
            data["scheduler"] = False

        return data

    def get_timezone(self):
        return self.timezone

    def fetch_site_info(self):
        data = {
            "config": self.config,
            "timezone": self.get_timezone(),
            "usage": self.get_usage(),
        }
        return data

    def fetch_site_analytics(self):
        return json.load(open(self.analytics_file))

    def sid(self, user="Administrator"):
        code = f"""import frappe
from frappe.app import init_request
try:
    from frappe.utils import set_request
except ImportError:
    from frappe.tests import set_request
set_request()
frappe.app.init_request(frappe.local.request)
frappe.local.login_manager.login_as("{user}")
print(">>>" + frappe.session.sid + "<<<")

"""

        output = self.bench_execute("console", input=code)["output"]
        return re.search(r">>>(.*)<<<", output).group(1)

    @property
    def timezone(self):
        query = (
            f"select defvalue from {self.database}.tabDefaultValue where"
            " defkey = 'time_zone' and parent = '__default'"
        )
        timezone = self.execute(
            f"mysql -h {self.host} -u{self.database} -p{self.password} "
            f'-sN -e "{query}"'
        )["output"].strip()
        return timezone

    @property
    def tables(self):
        return self.execute(
            "mysql --disable-column-names -B -e 'SHOW TABLES' "
            f"-h {self.host} -u {self.user} -p{self.password} {self.database}"
        )["output"].split("\n")

    @property
    def touched_tables(self):
        with open(self.touched_tables_file, "r") as f:
            return json.load(f)

    @job("Backup Site", priority="low")
    def backup_job(self, with_files=False, offsite=None):
        backup_files = self.backup(with_files)
        uploaded_files = (
            self.upload_offsite_backup(backup_files, offsite)
            if (offsite and backup_files)
            else {}
        )
        return {"backups": backup_files, "offsite": uploaded_files}

    def fetch_latest_backup(self, with_files=True):
        databases, publics, privates = [], [], []
        backup_directory = os.path.join(self.directory, "private", "backups")

        for file in os.listdir(backup_directory):
            path = os.path.join(backup_directory, file)
            if file.endswith("database.sql.gz") or file.endswith(
                "database-enc.sql.gz"
            ):
                databases.append(path)
            elif file.endswith("private-files.tar") or file.endswith(
                "private-files-enc.tar"
            ):
                privates.append(path)
            elif file.endswith("files.tar") or file.endswith("files-enc.tar"):
                publics.append(path)

        backups = {"database": {"path": max(databases, key=os.path.getmtime)}}

        if with_files:
            backups["private"] = {"path": max(privates, key=os.path.getmtime)}
            backups["public"] = {"path": max(publics, key=os.path.getmtime)}

        for backup in backups.values():
            file = os.path.basename(backup["path"])
            backup["file"] = file
            backup["size"] = os.stat(backup["path"]).st_size
            backup["url"] = f"https://{self.name}/backups/{file}"

        return backups

    def get_usage(self):
        """Returns Usage in bytes"""
        backup_directory = os.path.join(self.directory, "private", "backups")
        public_directory = os.path.join(self.directory, "public")
        private_directory = os.path.join(self.directory, "private")
        backup_directory_size = get_size(backup_directory)

        return {
            "database": b2mb(self.get_database_size()),
            "public": b2mb(get_size(public_directory)),
            "private": b2mb(
                get_size(private_directory) - backup_directory_size
            ),
            "backups": b2mb(backup_directory_size),
        }

    def get_analytics(self):
        analytics = self.bench_execute("execute frappe.utils.get_site_info")[
            "output"
        ]
        return json.loads(analytics)

    def get_database_size(self):
        # only specific to mysql/mariaDB. use a different query for postgres.
        # or try using frappe.db.get_database_size if possible
        query = (
            "SELECT SUM(`data_length` + `index_length`)"
            " FROM information_schema.tables"
            f' WHERE `table_schema` = "{self.database}"'
            " GROUP BY `table_schema`"
        )
        command = (
            f"mysql -sN -h {self.host} -u{self.user} -p{self.password}"
            f" -e '{query}'"
        )
        database_size = self.execute(command).get("output")

        try:
            return int(database_size)
        except Exception:
            return 0

    @property
    def job_record(self):
        return self.bench.server.job_record

    @property
    def step_record(self):
        return self.bench.server.step_record
