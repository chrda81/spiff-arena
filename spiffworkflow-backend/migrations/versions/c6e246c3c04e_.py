"""empty message

Revision ID: c6e246c3c04e
Revises: 6344d90d20fa
Create Date: 2024-02-19 16:41:52.728357

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c6e246c3c04e'
down_revision = '6344d90d20fa'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('message_triggerable_process_model', schema=None) as batch_op:
        batch_op.add_column(sa.Column('file_name', sa.String(length=255), nullable=True))
        batch_op.create_index(batch_op.f('ix_message_triggerable_process_model_file_name'), ['file_name'], unique=False)

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('message_triggerable_process_model', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_message_triggerable_process_model_file_name'))
        batch_op.drop_column('file_name')

    # ### end Alembic commands ###