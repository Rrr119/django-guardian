"""
Scale tests: object permissions must keep working on querysets of
100,000 objects - for bulk assignment, queryset filtering and the
permission checker cache.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase

from guardian.core import ObjectPermissionChecker
from guardian.models import GroupObjectPermission, UserObjectPermission
from guardian.shortcuts import assign_perm, get_objects_for_user
from guardian.testapp.models import Post

User = get_user_model()

OBJECT_COUNT = 100_000
USER_GRANTED_COUNT = 1_000
GROUP_GRANTED_COUNT = 1_000


class HundredThousandObjectsTest(TestCase):
    """Bulk assignment, filtering and caching over 100k objects."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="scale-user")
        cls.group = Group.objects.create(name="scale-group")
        cls.user.groups.add(cls.group)
        cls.posts = Post.objects.bulk_create(Post(title=f"post-{i}") for i in range(OBJECT_COUNT))
        # Every 100th object is granted directly to the user, every other
        # 100th to the user's group (1,000 objects each, no overlap).
        cls.user_posts = cls.posts[::100]
        cls.group_posts = cls.posts[1::100]
        UserObjectPermission.objects.bulk_assign_perm("view_post", cls.user, cls.user_posts)
        GroupObjectPermission.objects.bulk_assign_perm("view_post", cls.group, cls.group_posts)
        cls.granted_pks = {post.pk for post in cls.user_posts} | {post.pk for post in cls.group_posts}
        # Warm the one-time content-type cache so the tests below can assert
        # the steady-state query count of the permission filter itself.
        get_objects_for_user(cls.user, "testapp.view_post", Post.objects.all(), accept_global_perms=False).count()

    def test_bulk_assign_perm_at_scale(self):
        """Bulk-assigning a permission to all 100k objects must succeed."""
        assign_perm("change_post", self.user, Post.objects.all())
        self.assertEqual(
            UserObjectPermission.objects.filter(permission__codename="change_post").count(),
            OBJECT_COUNT,
        )
        self.assertEqual(
            get_objects_for_user(
                self.user,
                "testapp.change_post",
                Post.objects.all(),
                accept_global_perms=False,
            ).count(),
            OBJECT_COUNT,
        )

    def test_get_objects_for_user_filters_at_scale(self):
        """Filtering 100k objects by object permission happens in the
        database, with a constant number of queries."""
        with self.assertNumQueries(1):
            result = list(
                get_objects_for_user(
                    self.user,
                    "testapp.view_post",
                    Post.objects.all(),
                    accept_global_perms=False,
                )
            )
        self.assertEqual(len(result), USER_GRANTED_COUNT + GROUP_GRANTED_COUNT)
        self.assertEqual({post.pk for post in result}, self.granted_pks)

    def test_get_objects_for_user_without_groups_at_scale(self):
        with self.assertNumQueries(1):
            result = list(
                get_objects_for_user(
                    self.user,
                    "testapp.view_post",
                    Post.objects.all(),
                    use_groups=False,
                    accept_global_perms=False,
                )
            )
        self.assertEqual(len(result), USER_GRANTED_COUNT)
        self.assertEqual({post.pk for post in result}, {post.pk for post in self.user_posts})

    def test_prefetch_perms_caches_at_scale(self):
        """Prefetching 100k objects fills the cache; subsequent permission
        checks must not hit the database at all."""
        checker = ObjectPermissionChecker(self.user)
        checker.prefetch_perms(Post.objects.all())
        with self.assertNumQueries(0):
            granted = {post.pk for post in self.posts if checker.has_perm("view_post", post)}
        self.assertEqual(granted, self.granted_pks)
